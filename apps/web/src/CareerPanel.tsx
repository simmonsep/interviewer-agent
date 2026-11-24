import {useCallback,useEffect,useRef,useState,type FormEvent} from 'react';
import type {Theme} from './shell.ts';
import type {ResearchSelection} from './JobResearchPanel.tsx';
import {MaterialsPanel} from './MaterialsPanel.tsx';
import {ModelSettingsSection} from './ModelSettingsPanel.tsx';
import {AgentPolicySection} from './AgentPolicySection.tsx';
import {ConnectorsSection} from './ConnectorsSection.tsx';
import {InterviewWikiSection} from './InterviewWikiSection.tsx';
import {SkillsSection} from './SkillsSection.tsx';
import {StrategiesSection} from './StrategiesSection.tsx';
import {QuestionBanksPanel} from './QuestionBanksPanel.tsx';
import {WrittenPanel} from './WrittenPanel.tsx';
import {AlgorithmPracticePanel} from './AlgorithmPracticePanel.tsx';
import {ApplicationsPanel} from './ApplicationsPanel.tsx';
import {api,post,ApiError,describeError} from './api.ts';
import {getGoalPresentation,getWorkbenchPrimaryAction} from './workbenchCopy.ts';
import {getAsyncActionCopy} from './asyncActionCopy.ts';
import {onDataChanged} from './dataEvents.ts';
import {getPositionBrief,getBriefKeyPoints,type BriefItem,type BriefMatch} from './positionBrief.ts';
import {getCareerProgress,shouldExplainOptionalStages,OPTIONAL_STAGE_HINT} from './careerProgress.ts';
import {JobResearchPanel} from './JobResearchPanel.tsx';
import {InterviewPanel,type InterviewContext} from './InterviewPanel.tsx';
import {formatRemaining,statusLabel} from './interviewContracts.ts';
import {AgentConsole,AgentArtifacts,type AgentWorkflow,type AgentPane} from './AgentConsole.tsx';
import {AgentDock,type AgentKind} from './AgentDock.tsx';
import {AgentPipeline} from './AgentPipeline.tsx';
import {useRailSignals} from './railSignals.ts';
import {dragSurfaceProps,hasWindowControl,railCaptionProps,windowAction,windowState} from './windowControl.ts';

interface Conditions{direction:string;cities:string[];city_policy:'unknown'|'preferred'|'required';auto_research?:boolean;recruitment?:string;graduation_year?:number|null;days_per_week?:number|null;duration_months?:number|null;skills?:string[]}
interface Extract{value:string;message_id:string;quote:string}
interface Goal{
 id:string;version:number;revision:number;status:string;conditions:Conditions;jd_text:string;model_requests:number;
 profile:{direction:Extract|null;cities:Extract[];recruitment:Extract|null}|null;
 action:{type:string;text:string}|null;
 steps:{id:string;goal_version:number;message:string;status:string;error_code:string|null;origin:string|null;model_requests:number;tool_log:{tool:string;status:string}[]|null}[];
}
interface Command{goalId:string|null;key:string;body:Record<string,unknown>}
type briefItems=BriefItem[];
interface briefRun{id:string;result:{matches?:BriefMatch[]}|null}
type briefRuns=briefRun[];
const UUID_RE=/^[a-f0-9-]{36}$/i;
const phases:Record<string,string>={PENDING:'等待协调',RUNNING:'正在梳理目标',WAITING_FOR_USER:'等待你补充',WAITING_FOR_TOOL:'下一步请求已准备',WAITING_CREDENTIALS:'模型调用已暂停',FAILED:'本次协调未完成',CANCELLED:'已结束',COMPLETED:'本轮已完成',SUCCEEDED:'已处理'};

// A pane that needs a goal used to offer only "去创建工作目标", so a user who already had a
// goal but had never opened it inside the desktop shell saw an empty pane and no way in.
// Listing the existing goals makes "no goal selected" different from "no goal exists".
function GoalNeeded({title,note,goals,busy,onChoose,onCreate}:{
 title:string;note:string;busy:boolean;
 goals:{id:string;direction:string;status:string}[];
 onChoose:(id:string)=>void;onCreate:()=>void;
}){
 return <section className="card d-card d-empty">
  <h1>{title}</h1>
  <p className="muted">{note}</p>
  {goals.length>0&&<>
   <p className="muted">本机已经保存了 {goals.length} 个求职目标，选一个继续：</p>
   <div className="d-goal-list" role="list" aria-label="选择已有求职目标">
    {goals.slice(0,6).map(item=>
     <button key={item.id} type="button" role="listitem" className="d-goal-item" disabled={busy}
      title={item.direction||'未命名目标'} onClick={()=>onChoose(item.id)}>
      <span>{item.direction||'未命名目标'}</span>
      <small>{phases[item.status]??item.status}</small>
     </button>)}
   </div>
  </>}
  <button type="button" className="primary" disabled={busy} onClick={onCreate}>去创建工作目标</button>
 </section>;
}

type HostCore={invoke<T>(command:string,args?:Record<string,unknown>):Promise<T>};
/** The Tauri bridge, when this window is the desktop app rather than a browser tab. */
function hostCore():HostCore|undefined{
 return (window as unknown as {__TAURI__?:{core?:HostCore}}).__TAURI__?.core;
}

type CurrentPrep={id:string;status:string;session_id:string|null;error_code:string|null;
 version:number;plan_sha256:string|null};

export function CareerPanel({userId,onAuthLost,variant='web',accountName,accountEmail,onLogout,onSwitchAccount,appearance='light',appearanceChoice='light',onAppearanceChange,assessmentMode='scripted'}:{
 userId:string;onAuthLost:()=>void;variant?:'web'|'desktop';accountName?:string;accountEmail?:string;
 onLogout?:()=>void;onSwitchAccount?:()=>void;
 appearance?:'light'|'dark';appearanceChoice?:Theme;onAppearanceChange?:(next:Theme)=>void;
 assessmentMode?:'scripted'|'live';
}){
 // 侧栏展开/收起：与 panes 的 sidebarPin 同一思路，状态落在本机，重启保持。
 const [railOpen,setRailOpen]=useState(()=>{try{return localStorage.getItem('py-desktop-rail')!=='collapsed';}catch{return true;}});
 // The host injects its bridge after the page starts loading, so the first render
 // may run before it exists. Poll briefly, then stop: in a browser tab it never
 // appears and the window controls must stay hidden.
 const [windowBridge,setWindowBridge]=useState(false);
 // The maximise button shows "maximise" or "restore" depending on the window's
 // real state, so pressing it always produces a visible change.
 const [maximized,setMaximized]=useState(false);
 useEffect(()=>{
  if(hasWindowControl()){setWindowBridge(true);return;}
  let tries=0;
  const timer=window.setInterval(()=>{
   if(hasWindowControl()){setWindowBridge(true);window.clearInterval(timer);return;}
   if(++tries>=40){window.clearInterval(timer);}
  },100);
  return()=>window.clearInterval(timer);
 },[]);
 useEffect(()=>{
  if(!windowBridge)return;
  let alive=true;
  const sync=()=>{void windowState().then(state=>{if(alive&&state)setMaximized(state.maximized);});};
  sync();
  window.addEventListener('resize',sync);
  const timer=window.setInterval(sync,1500);
  return()=>{alive=false;window.removeEventListener('resize',sync);window.clearInterval(timer);};
 },[windowBridge]);
 function toggleRail(){setRailOpen(open=>{const next=!open;try{localStorage.setItem('py-desktop-rail',next?'open':'collapsed');}catch{}return next;})}
 const storageKey=`py-career:${userId}:command`;
 // Desktop shell only: reopen the goal the user was last working on. A native window
 // starts without ?goal, so without this the workbench would come back empty.
 const lastGoalKey=`py-desktop-goal:${userId}`;
 const [goal,setGoal]=useState<Goal|null>(null),[ids,setIds]=useState<string[]>([]),[message,setMessage]=useState(''),[jd,setJd]=useState(''),[direction,setDirection]=useState(''),[cities,setCities]=useState(''),[policy,setPolicy]=useState<Conditions['city_policy']>('unknown');
 const [error,setError]=useState(''),[busy,setBusy]=useState(false);
 const [autoResearch,setAutoResearch]=useState(false),[recruitment,setRecruitment]=useState('unknown'),[graduation,setGraduation]=useState(''),[days,setDays]=useState(''),[months,setMonths]=useState(''),[skills,setSkills]=useState('');
 // F07Z-166: "this session uses no resume" is a real choice, passed with the training turn.
 const [noResume,setNoResume]=useState(false);
 const current=useRef<Goal|null>(null),generation=useRef(0),sequence=useRef(0),lock=useRef(false),draftVersion=useRef(0),pending=useRef<Command|null>(null);
 // Desktop shell only: which pane is open, the interview line in the context bar and
 // the one-shot request to open a specific session from the workbench list.
 const desktop=variant==='desktop';
 const [pane,setPane]=useState<'workbench'|'artifacts'|'interview'|'written'|'research'|'materials'|'apps'|'banks'|'settings'>('workbench');
 // F07Z-143: switching panes keeps the old scroll offset, so a long review could hand the user a
 // page that opens halfway down. Each pane opens at its own top (title + target name first).
 useEffect(()=>{
  const body=document.querySelector('.desktop-body') as HTMLElement|null;
  body?.scrollTo?.({top:0,behavior:'auto'});
  window.scrollTo?.({top:0,behavior:'auto'});
 },[pane]);
 const [ctx,setCtx]=useState<InterviewContext|null>(null);
 const [openSessionId,setOpenSessionId]=useState<string|null>(null);
 const [now,setNow]=useState(()=>Date.now());
 const [formOpen,setFormOpen]=useState(false);
 // F07Z-166: the same form saves conditions or asks for training; only the latter may
 // start a preparation on the server.
 const submitMode=useRef<'training'|'save'>('save');
 // F07Z-141: whether the optional conditions are expanded. Desktop keeps them open (the form
 // used to show them inline); a phone starts collapsed so the core field gets the first screen.
 // This is presentation only - no new business state, no wizard.
 const [goalMoreOpen,setGoalMoreOpen]=useState(()=>{
  try{return !window.matchMedia('(max-width:560px)').matches;}catch{return true;}});
 // Mirrored from the research pane so the context bar can name the chosen job; the
 // server owns the selection, this is display only.
 const [selection,setSelection]=useState<ResearchSelection|null>(null);
 const [goals,setGoals]=useState<{id:string;direction:string;status:string;version:number}[]>([]);
 // The agent's run artifacts are fetched once by the console and shared with the
 // side rail, so the two views never disagree about what the agent has produced.
 const [agentFlow,setAgentFlow]=useState<AgentWorkflow|null>(null);
 // F07Z-89: the question-bank page can start a session; this is the id the written pane opens.
 const [writtenRequest,setWrittenRequest]=useState<string|null>(null);
 // Version B: the three fixed role Agents are entered from the rail/dock and pin
 // a real backend mode on their own panel. interview/coaching share /interviews;
 // resume rewrite pins the materials panel to /materials/rewrite.
 const [agentMode,setAgentMode]=useState<'interview'|'coaching'>('interview');
 const [resumeFocus,setResumeFocus]=useState(false);
 const formRef=useRef<HTMLTextAreaElement|null>(null);
 // B+ : the one-sentence Agent entry lives in the top bar (always available),
 // while the eight rail entries show real result counts instead of task names.
 const omniRef=useRef<HTMLInputElement|null>(null);
 const signals=useRailSignals(goal);
 const receiveContext=useCallback((value:InterviewContext)=>setCtx(value),[]);
 // F07Z-142: the preparation chain is derived from facts the panels already hold. Two cheap
 // local GETs (materials, applications) feed it; a failure leaves it null and the workbench
 // keeps the previous single "start preparation" button instead of guessing at state.
 const [chain,setChain]=useState<{materials:number;drafts:number;applications:number;selectedJob:boolean;
  jobKeyPoints:{title:string;skills:string[]}|null}|null>(null);
 // Bumped whenever the user returns to the workbench so the chain is re-read even when the
 // goal version did not change (materials carry their own revision).
 const [chainTick,setChainTick]=useState(0);
 // Set after a successful create/message submit so the card can say what happens next.
 const [goalJustCreated,setGoalJustCreated]=useState(false);
 useEffect(()=>{
  if(pane!=='workbench')return;
  void refresh();
  setChainTick(value=>value+1);
 },[pane]);
 // F07Z-148: panels announce real writes; the workbench re-reads goal + chain without a reload
 // and without the user pressing 刷新目标.
 useEffect(()=>onDataChanged(area=>{
  if(area==='goal')return;
  void refresh();
  setChainTick(value=>value+1);
 }),[]);
 useEffect(()=>{
  let alive=true;
  if(!goal){setChain(null);return;}
  void (async()=>{
   try{
    const [materials,applications,jobs]=await Promise.all([
     api<{sources:unknown[];drafts:unknown[]}>(`/api/career-goals/${goal.id}/materials`),
     api<{items:unknown[]}>(`/api/career-goals/${goal.id}/applications`).catch(()=>({items:[]})),
     // F07Z-145: the applications form refuses to save without a selected job, so the entry
     // decision needs the server's answer, not a guess from the research pane's local state.
     api<{selected?:{snapshot_id:string}|null;items:briefItems;runs:briefRuns}>(
      `/api/career-goals/${goal.id}/jobs`).catch(()=>null),
    ]);
    // F07Z-151: the materials page and the interview setup read the SAME brief the research page
    // shows, derived from the selected snapshot the server already stored.
    const selected=jobs?.selected;
    const briefItem=selected?jobs?.items?.find(item=>item.snapshot_id===selected.snapshot_id):undefined;
    const briefRun=briefItem?jobs?.runs?.find(run=>run.id===briefItem.run_id):undefined;
    const briefMatch=briefItem
     ?briefRun?.result?.matches?.find(match=>match.snapshot_id===briefItem.snapshot_id)??null
     :null;
    const keyPoints=briefItem?getBriefKeyPoints(getPositionBrief({item:briefItem,match:briefMatch})):null;
    if(alive)setChain({materials:materials.sources.length,drafts:materials.drafts.length,
     applications:applications.items.length,selectedJob:Boolean(selected),jobKeyPoints:keyPoints});
   }catch{if(alive)setChain(null);}
  })();
  return()=>{alive=false;};
 },[goal?.id,goal?.version,chainTick]);
 useEffect(()=>{
  if(!desktop||!ctx?.deadlineAt)return;
  const timer=window.setInterval(()=>setNow(Date.now()),1000);
  return()=>window.clearInterval(timer);
 },[desktop,ctx?.deadlineAt]);
 const accept=(value:Goal|null)=>{current.current=value;setGoal(value);};
 const extra=(c:Conditions)=>{setAutoResearch(c.auto_research??false);setRecruitment(c.recruitment??'unknown');setGraduation(c.graduation_year?.toString()??'');setDays(c.days_per_week?.toString()??'');setMonths(c.duration_months?.toString()??'');setSkills(c.skills?.join('、')??'');};
 const fill=(value:Goal)=>{setDirection(value.conditions.direction);setCities(value.conditions.cities.join('、'));setPolicy(value.conditions.city_policy);setJd(value.jd_text);extra(value.conditions);draftVersion.current=value.version;};
 function failure(e:unknown){if(e instanceof ApiError&&e.status===401)onAuthLost();else if(e instanceof ApiError&&e.status===400)setError('请检查意向、方向和城市长度；JD最多2500字。');else if(e instanceof ApiError&&e.status===404)setError('这个求职目标不存在，或不属于当前账号。');else setError(describeError(e));}
 async function history(){
  const value=await api<{items:string[]}>('/api/career-goals/');
  setIds(value.items);
  // The listing endpoint returns ids only; the switch list needs a label per goal.
  const details=await Promise.all(value.items.slice(0,6).map(id=>api<Goal>(`/api/career-goals/${id}`).catch(()=>null)));
  setGoals(details.filter((item):item is Goal=>Boolean(item)).map(item=>({id:item.id,direction:item.conditions.direction,status:item.status,version:item.version})));
 }
 // F07Z-151 (arbiter Q3=C): a deep link names a goal. Until that read finishes the workbench must
 // not show the create form, and a failed read must not silently degrade into it either.
 const [goalLoading,setGoalLoading]=useState(false);
 const [goalLoadError,setGoalLoadError]=useState<string|null>(null);
 const [forceCreate,setForceCreate]=useState(false);
 // 收尾 02-1: a failed read keeps the id it actually tried. Retry must reuse that id instead of
 // re-deriving one from the URL or localStorage, and it must never fall back to another goal.
 const [failedGoalId,setFailedGoalId]=useState<string|null>(null);
 const goalRoutePending=!goal&&(goalLoading||(Boolean(goalLoadError)&&!forceCreate));
 async function choose(id:string){
  const epoch=++generation.current;accept(null);setError('');
  setGoalLoadError(null);setForceCreate(false);setGoalLoading(true);setFailedGoalId(null);
  try{const value=await api<Goal>(`/api/career-goals/${id}`);if(epoch!==generation.current)return;accept(value);fill(value);if(pending.current?.goalId===id)restore(pending.current);
  // F07Z-150: keep the other locator params (shell / interview). Rewriting the URL to a bare
  // `?goal=` used to drop a deep link on every reload.
  try{const url=new URL(window.location.href);url.searchParams.set('goal',id);window.history.replaceState(null,'',url);}catch{window.history.replaceState(null,'',`/?goal=${id}`);}}
  catch(e){if(epoch===generation.current){failure(e);setGoalLoadError(goalLoadFailureText(e));setFailedGoalId(id);}}
  finally{if(epoch===generation.current)setGoalLoading(false);}
 }
 function goalLoadFailureText(e:unknown){
  if(e instanceof ApiError&&e.status===404)return '这个求职目标不存在，或不属于当前账号。';
  if(e instanceof ApiError&&e.status===401)return '登录已过期，请重新登录。';
  return describeError(e);
 }
 const retryGoalLoad=()=>{
  // The id that just failed wins. Only when there is none (an old in-memory error) do we look
  // at the locator, and we never switch to a different historical goal on the user's behalf.
  let fromUrl='';
  try{fromUrl=new URLSearchParams(window.location.search).get('goal')??'';}catch{fromUrl='';}
  const target=failedGoalId&&UUID_RE.test(failedGoalId)?failedGoalId
   :(UUID_RE.test(fromUrl)?fromUrl:'');
  // Nothing to retry: keep the error surface. Clearing it would reveal the create form, which is
  // exactly the silent degradation this surface exists to prevent.
  if(target)void choose(target);
 };
 const createInstead=()=>{setForceCreate(true);setGoalLoadError(null);setFailedGoalId(null);};
 const goalRouteNotice=goalLoading
  ?<div className="d-goal-route-state" role="status" data-testid="goal-loading">
    <h2 data-testid="workbench-goal">正在读取目标…</h2>
    <p className="muted">链接里的目标还没有读回来，请稍候；这期间不会显示创建表单。</p></div>
  :<div className="d-goal-route-state" role="alert" data-testid="goal-load-error">
    <h2 data-testid="workbench-goal">目标没有读出来</h2>
    <p className="muted">{goalLoadError}</p>
    <div className="d-form-foot">
     <button type="button" className="secondary" data-testid="goal-load-retry" onClick={retryGoalLoad}>重试读取</button>
     <button type="button" className="text-button" data-testid="goal-load-create" onClick={createInstead}>改为新建目标</button>
    </div></div>;
 function restore(saved:Command){
  setMessage(String(saved.body.message??''));setJd(String(saved.body.jd_text??''));
  const c=saved.body.conditions as Conditions|undefined;
  if(c){setDirection(String(c.direction??''));setCities(Array.isArray(c.cities)?c.cities.join('、'):'');setPolicy(c.city_policy??'unknown');extra(c);}
 }
 async function refresh(){
  const before=current.current;if(!before)return;const epoch=generation.current,request=++sequence.current;
  try{const value=await api<Goal>(`/api/career-goals/${before.id}`);
   if(epoch===generation.current&&request===sequence.current&&current.current?.id===value.id&&value.revision>=current.current.revision)accept(value);
  }catch(e){if(epoch===generation.current)failure(e);}
 }
 useEffect(()=>{
  let alive=true;
  try{const saved=JSON.parse(sessionStorage.getItem(storageKey)??'null') as Command|null;
   if(saved?.body&&typeof saved.key==='string'){pending.current=saved;restore(saved);}}
  catch{sessionStorage.removeItem(storageKey);}
  // `ids` alone is not enough: the "pick an existing goal" list needs a label per goal, and
  // the desktop shell never called `history()` on load, so the list was empty and every pane
  // fell back to "去创建工作目标" even when goals existed.
  void history().catch(e=>{if(alive)failure(e);});
  const id=new URLSearchParams(window.location.search).get('goal');if(id&&/^[a-f0-9-]{36}$/i.test(id))void choose(id);
  else if(desktop){try{const last=localStorage.getItem(lastGoalKey);if(last&&/^[a-f0-9-]{36}$/i.test(last))void choose(last);}catch{}}
  return()=>{alive=false;generation.current++;};
 },[userId]);
 useEffect(()=>{if(!desktop||!goal)return;try{localStorage.setItem(lastGoalKey,goal.id);}catch{}},[desktop,goal?.id]);
 useEffect(()=>{if(!goal||!['PENDING','RUNNING'].includes(goal.status))return;const timer=setInterval(()=>void refresh(),1000);return()=>clearInterval(timer);},[goal?.id,goal?.status]);
 async function submit(event:FormEvent){
  event.preventDefault();if(lock.current)return;lock.current=true;setBusy(true);setError('');
  const before=current.current,epoch=generation.current;
  const conditions:Conditions={direction,cities:cities.split(/[、,，]/).map(x=>x.trim()).filter(Boolean),city_policy:policy,auto_research:autoResearch,
   recruitment,graduation_year:graduation?Number(graduation):null,days_per_week:days?Number(days):null,duration_months:months?Number(months):null,skills:skills.split(/[、,，]/).map(x=>x.trim()).filter(Boolean)};
  const training=submitMode.current==='training';
  submitMode.current='save';
  const fallback=training?'按我确认的方向和 JD 准备一场面试。':'保存以下求职条件与 JD，供后续准备训练使用。';
  const body={message:message.trim()?message:fallback,jd_text:jd,conditions,
   ...(training?{intent:'training' as const}:{}),
   ...(training?{use_resume:!noResume}:{}),
   request:training?'training' as const:'save_jd' as const,
   ...(before?{expected_version:draftVersion.current}:{})};
  const comparable=(value:Record<string,unknown>)=>{const copy={...value};delete copy.expected_version;return JSON.stringify(copy);};
  if(!pending.current||pending.current.goalId!==(before?.id??null)||comparable(pending.current.body)!==comparable(body))
   pending.current={goalId:before?.id??null,key:crypto.randomUUID(),body};
  sessionStorage.setItem(storageKey,JSON.stringify(pending.current));
  try{
   const result=await post<Goal>(before?`/api/career-goals/${before.id}/messages`:'/api/career-goals/',pending.current.body,pending.current.key);
   pending.current=null;sessionStorage.removeItem(storageKey);
   if(epoch===generation.current){accept(result);fill(result);setMessage('');setFormOpen(false);setGoalJustCreated(true);window.history.replaceState(null,'',`/?goal=${result.id}`);await history();}
  }catch(e){
   if(epoch===generation.current){failure(e);if(e instanceof ApiError&&e.status===409){pending.current=null;sessionStorage.removeItem(storageKey);if(before)await choose(before.id);setError('目标或请求已更新，请核对最新条件后重试。');}}
  }finally{lock.current=false;setBusy(false);}
 }
 function newGoal(){
  generation.current++;current.current=null;setGoal(null);setSelection(null);
  setMessage('');setJd('');setDirection('');setCities('');setPolicy('unknown');setNoResume(false);
  extra({direction:'',cities:[],city_policy:'unknown'});draftVersion.current=0;pending.current=null;
  sessionStorage.removeItem(storageKey);
  window.history.replaceState(null,'',`/?shell=desktop`);
  setPane('workbench');setFormOpen(true);
 }
 async function cancel(){const before=current.current;if(!before||lock.current)return;lock.current=true;setBusy(true);const epoch=generation.current;try{
  const value=await post<Goal>(`/api/career-goals/${before.id}/cancel`,{expected_version:before.version});if(epoch===generation.current)accept(value);
 }catch(e){if(epoch===generation.current)failure(e);}finally{lock.current=false;setBusy(false);}}
 const last=goal?.steps.at(-1);
 const provenance=last?.origin==='real_model'?'模型协调预览 · 提取结果待你确认':last?.origin==='scripted_protocol'?'脚本流程演示 · 不代表真实意图识别效果':'你的输入已保存。';

 const goalCard=goal&&<div className="goal-card" aria-label="当前求职目标"><strong role="status">{phases[goal.status]??goal.status}</strong>
  <p>已确认方向：{goal.conditions.direction||'尚未确认'}；城市：{goal.conditions.cities.join('、')||'尚未确认'}</p>
  {goal.profile&&<div><h3>待确认的提取建议</h3><p>方向：{goal.profile.direction?.value??'未知'}；城市：{goal.profile.cities.map(v=>v.value).join('、')||'未知'}；招聘类型：{goal.profile.recruitment?.value??'未知'}</p>
   <button className="text-button" disabled={busy} onClick={()=>{setDirection(goal.profile?.direction?.value??direction);setCities(goal.profile?.cities.map(x=>x.value).join('、')||cities);}}>将建议填入下方，确认后再保存</button></div>}
  {goal.action&&<p className="career-action">{goal.action.text}</p>}
  {goal.status==='WAITING_FOR_TOOL'&&<p className="notice">{goal.conditions.auto_research&&goal.action?.type==='prepare_job_research'?'研究提议已提交，后台研究Agent将继续；结果会显示在下方。':'需求已保存。可在下方读取岗位；开启自动研究可由研究Agent继续，整场训练将在后续阶段接入。'}</p>}
  {goal.status==='WAITING_FOR_USER'&&<p className="notice">这一步需要你补充信息：在上方表单回答后保存，后台会接着协调。</p>}
  {goal.status==='WAITING_CREDENTIALS'&&<p className="notice">模型凭据、授权或额度尚未就绪，已停止调用。输入仍保留；完成本机配置后可发送新消息继续。</p>}
  {last?.tool_log&&<p className="fine-print">本轮动作：{last.tool_log.map(t=>({read_goal:'读取目标',ask_user:'提出澄清',prepare_job_research:'准备岗位研究',prepare_training:'准备训练',finish:'结束'}[t.tool]??'校验动作')+(t.status==='error'?'（未通过校验）':'')).join(' → ')}</p>}
  <button className="text-button" disabled={busy} onClick={()=>void refresh()}>刷新目标</button>
  {goal.status!=='CANCELLED'&&<button className="text-button" disabled={busy} onClick={()=>void cancel()}>结束此目标</button>}
 </div>;

 const goalForm=(withSubmit=true)=><form id="desktop-goal-form" onSubmit={event=>void submit(event)}>
  <label>求职意向或补充说明<textarea ref={formRef} aria-label="求职意向或补充说明" maxLength={2000} value={message} onChange={e=>setMessage(e.target.value)} placeholder="例如：研二，想在上海找 AI 应用实习，暂时不确定具体方向。"/></label>
  {/* F07Z-141: one sentence is enough to start, so the optional fields sit behind a native
      disclosure on first use. Desktop keeps it open by default; a phone starts collapsed
      (§24/§57) - and because it is <details>, keyboard and no-JS behaviour come for free. */}
  <details className="d-goal-more" open={goalMoreOpen} onToggle={e=>{if(e.target===e.currentTarget)setGoalMoreOpen((e.target as HTMLDetailsElement).open);}}>
   <summary>更多条件（可选）</summary>
   <div className="career-fields"><label>确认方向（可选）<input maxLength={80} value={direction} onChange={e=>setDirection(e.target.value)}/></label><label>确认城市（可选，多个用顿号分隔）<input value={cities} onChange={e=>setCities(e.target.value)}/></label>
    <label>城市要求<select value={policy} onChange={e=>setPolicy(e.target.value as Conditions['city_policy'])}><option value="unknown">尚未确定</option><option value="preferred">优先考虑</option><option value="required">必须满足</option></select></label></div>
   <details><summary>补充匹配条件（可留空，无简历也能使用）</summary><div className="career-fields">
    <label>招聘类型<select aria-label="招聘类型" value={recruitment} onChange={e=>setRecruitment(e.target.value)}><option value="unknown">尚未确认</option><option value="intern">实习</option><option value="campus">应届校招</option><option value="experienced">社会招聘</option></select></label>
    <label>毕业年份<input type="number" min={2000} max={2100} value={graduation} onChange={e=>setGraduation(e.target.value)}/></label>
    <label>每周可到岗天数<input type="number" min={1} max={7} value={days} onChange={e=>setDays(e.target.value)}/></label>
    <label>可实习月数<input type="number" min={1} max={24} value={months} onChange={e=>setMonths(e.target.value)}/></label>
   </div><label>自述技能（多个用顿号分隔）<input maxLength={800} value={skills} onChange={e=>setSkills(e.target.value)}/></label><p className="fine-print">用于对照岗位原文。未声明不代表不会，自述不等于经过能力验证。</p></details>
  </details>
  <label className="check-label"><input type="checkbox" checked={autoResearch} onChange={e=>setAutoResearch(e.target.checked)}/>自动研究并匹配（受限公开来源）</label>
  <label className="check-label"><input type="checkbox" data-testid="no-resume-choice" checked={noResume} onChange={e=>setNoResume(e.target.checked)}/>本场不使用绑定简历（只按岗位训练）</label>
  {noResume&&<p className="fine-print" data-testid="no-resume-hint">当前没有同版本的绑定简历时，勾选只表示本场不使用简历；之后即使出现可用简历也不会自动回填。</p>}
  <label>职位说明 JD（可选，最多2500字）<textarea aria-label="职位说明 JD" maxLength={2500} value={jd} onChange={e=>setJd(e.target.value)}/></label>
  {withSubmit&&<div className="d-form-foot">
   <button type="submit" className="text-button" disabled={busy||goal?.status==='CANCELLED'}
    onClick={()=>{submitMode.current='save';}}>{busy?'正在保存…':'只保存条件与 JD'}</button>
   <button type="submit" className="primary" disabled={busy||goal?.status==='CANCELLED'||(!message.trim()&&!jd.trim()&&!direction.trim())}
    onClick={()=>{submitMode.current='training';}}>开始准备这场面试</button>
  </div>}
 </form>;

 if(!desktop){
  return <section className="career-layout">
   <aside className="career-history"><h2>求职目标</h2><button className="text-button" disabled={busy} onClick={()=>{generation.current++;current.current=null;setGoal(null);setMessage('');setJd('');setDirection('');setCities('');setPolicy('unknown');extra({direction:'',cities:[],city_policy:'unknown'});draftVersion.current=0;pending.current=null;sessionStorage.removeItem(storageKey);window.history.replaceState(null,'','/');}}>新建求职目标</button>
    {ids.slice(0,8).map((id,index)=><button key={id} className="history-item" disabled={busy} onClick={()=>void choose(id)}>目标 {ids.length-index} · {id.slice(0,8)}</button>)}
   </aside><div className="career-main">
    <section className="card"><span className="eyebrow">YOUR NEXT STEP</span><h1>从求职意向开始</h1><p>方向还不清楚也可以开始；已有职位说明，可以直接准备训练。简历不是前置条件。</p>
     <p className="fine-print">{provenance}</p>{error&&<p role="alert" className="error">{error}</p>}
     {!goal&&new URLSearchParams(window.location.search).get('view')==='interview'&&<p className="notice">整场面试需要求职目标：请先在上方保存一个目标，再从左侧历史选中它。</p>}
     {goalRoutePending?goalRouteNotice:goalCard}
     {!goalRoutePending&&goalForm()}
    </section>
    {goal&&<InterviewPanel key={goal.id} userId={userId} goalId={goal.id} goalVersion={goal.version} goalStatus={goal.status} assessmentMode={assessmentMode} onAuthLost={onAuthLost} onGoalRefresh={()=>void refresh()}/>}
    {goal&&<JobResearchPanel key={`${goal.id}:${goal.version}`} userId={userId} goalId={goal.id} version={goal.version} jdText={goal.jd_text} direction={goal.conditions.direction} cancelled={goal.status==='CANCELLED'} goalStatus={goal.status} automatic={goal.conditions.auto_research??false} onGoalRefresh={()=>void refresh()} onAuthLost={onAuthLost}/>}
    {goal&&<section className="card"><h2>原始输入记录</h2>{goal.steps.map(step=><article className="career-message" key={step.id}><p style={{whiteSpace:'pre-wrap'}}>{step.message}</p><small>{phases[step.status]??step.status}</small></article>)}</section>}
   </div>
  </section>;
 }

 // ---- Desktop shell -------------------------------------------------------
 // Same state, same handlers, same InterviewPanel instance: the panes only change
 // what is visible. Progress / countdown / status appear once, in the context bar.
 const activeSession=ctx?.history.find(item=>!['COMPLETED','FAILED','CANCELLED'].includes(item.status))??null;
 const terminal=ctx?.status?['COMPLETED','FAILED','CANCELLED'].includes(ctx.status):false;
 const answering=ctx?.status==='WAITING_FOR_USER';
 const remaining=ctx?.deadlineAt?Math.max(0,ctx.deadlineAt-now):0;
 // F07Z-141: one presentation layer decides the words and the single primary action; the
 // handlers below are unchanged, the helper only picks which existing one to point at.
 const goalView=getGoalPresentation(goal,selection?.title);
 // F07Z-145: chain is the server's view; `selection` is the research pane's mirror. Prefer the
 // former and fall back to the latter while the first fetch is still in flight.
 const hasSelectedJob=chain?.selectedJob??Boolean(selection);
 const jobKeyPoints=chain?.jobKeyPoints??null;
 const [applicationIntent,setApplicationIntent]=useState(false);
 // F07Z-150: one stable locator for a session. The URL carries only the goal + session ids
 // (never the answers, the target name or any other content).
 const openSessionAt=(id:string)=>{
  setOpenSessionId(id);setPane('interview');
  try{
   const url=new URL(window.location.href);
   url.searchParams.set('interview',id);
   if(goal)url.searchParams.set('goal',goal.id);
   window.history.pushState(null,'',url);
  }catch{/* a blocked history must not block opening the session */}
 };
 // A direct link, a reload and Back all end up here: read the URL, then let the panel fetch.
 useEffect(()=>{
  if(!goal)return;
  let id='';
  try{id=new URLSearchParams(window.location.search).get('interview')??'';}catch{return;}
  if(UUID_RE.test(id)){setOpenSessionId(id);setPane('interview');}
  const onPop=()=>{
   let next='';
   try{next=new URLSearchParams(window.location.search).get('interview')??'';}catch{next='';}
   if(UUID_RE.test(next)){setOpenSessionId(next);setPane('interview');}
   else{setOpenSessionId(null);setPane('workbench');}
  };
  window.addEventListener('popstate',onPop);
  return()=>window.removeEventListener('popstate',onPop);
 },[goal?.id]);
 // Leaving toward the workbench drops the session from the URL, so Back/Forward walk between
 // "a session" and "the surface I came from" instead of between two copies of the same link.
 // Only a real transition counts: on first mount the pane is already 'workbench' and the deep
 // link in the URL has not been applied yet.
 const previousPane=useRef(pane);
 useEffect(()=>{
  const cameFrom=previousPane.current;
  previousPane.current=pane;
  if(cameFrom==='workbench'||pane!=='workbench')return;
  try{
   const url=new URL(window.location.href);
   if(!url.searchParams.has('interview'))return;
   url.searchParams.delete('interview');
   window.history.replaceState(null,'',url);
  }catch{/* nothing to clean up */}
 },[pane]);
 // A session can also be created inside the panel (no user click on a list). Keep the locator in
 // sync with what is actually open, without adding a history entry the user never asked for.
 useEffect(()=>{
  if(pane!=='interview'||!ctx?.sessionId)return;
  try{
   const url=new URL(window.location.href);
   if(url.searchParams.get('interview')===ctx.sessionId)return;
   url.searchParams.set('interview',ctx.sessionId);
   if(goal)url.searchParams.set('goal',goal.id);
   window.history.replaceState(null,'',url);
  }catch{/* nothing to sync if history is blocked */}
 },[pane,ctx?.sessionId,goal?.id]);
 const primaryLabel=getWorkbenchPrimaryAction({hasGoal:Boolean(goal),
  activeSession:Boolean(activeSession),sessionIsOpen:Boolean(activeSession&&ctx?.sessionId===activeSession.id)}).label;
 function primaryAction(){
  if(!goal){setPane('workbench');setFormOpen(true);window.setTimeout(()=>formRef.current?.focus(),0);return;}
  if(activeSession&&ctx?.sessionId!==activeSession.id)openSessionAt(activeSession.id);
  setPane('interview');
 }
 // The agent console opens a产物 pane only as a detail view; it is not the main path.
 const openAgentPane=(next:AgentPane)=>setPane(next);
 const focusAgentInput=()=>{setPane('workbench');setFormOpen(true);
  window.setTimeout(()=>{omniRef.current?.focus();omniRef.current?.scrollIntoView({block:'nearest'});},0);};
 // Two fixed Agent interfaces. 面试 owns both modes and the setup card picks one; the
 // shell no longer pins a mode when you enter, so choosing 陪练 on the card does not have
 // to survive a detour through another panel.
 const launchAgent=(kind:AgentKind)=>{
  if(kind==='resume'){setResumeFocus(true);setPane('materials');return;}
  setPane('interview');
 };
 // F07Z-142: one derived answer to "what is next", used by both the progress row and the
 // Next Action card. It reads existing data only; the handlers below stay untouched.
 const finished=ctx?.history.filter(item=>item.status==='COMPLETED')??[];
 const [prepState,setPrepState]=useState<CurrentPrep|null>(null);
 useEffect(()=>{
  if(!goal){setPrepState(null);return;}
  let alive=true,timer=0;
  // While the coordinator is turning this message into a proposal, the preparation row may
  // appear at any moment; keep reading until it does instead of requiring a manual refresh.
  const coordinating=['PENDING','RUNNING'].includes(goal.status);
  const load=async()=>{
   try{
    const value=await api<CurrentPrep>(`/api/career-goals/${goal.id}/interview-preparations/current`);
    if(!alive)return;
    setPrepState(value);
    if(value.status==='PENDING'||value.status==='RUNNING'||coordinating)timer=window.setTimeout(()=>void load(),900);
   }catch(e){
    if(!alive)return;
    if(e instanceof ApiError&&e.status===404){setPrepState(null);if(coordinating)timer=window.setTimeout(()=>void load(),900);}
   }
  };
  void load();
  const refresh=()=>void load();
  window.addEventListener('py-agent-result',refresh);
  return()=>{alive=false;if(timer)window.clearTimeout(timer);window.removeEventListener('py-agent-result',refresh);};
 },[goal?.id,goal?.status]);
 const progress=chain?getCareerProgress({
  hasGoal:Boolean(goal),materialCount:chain.materials,draftCount:chain.drafts,
  completedSessions:finished.length,answeredQuestions:finished.reduce((sum,item)=>sum+item.answers,0),
  applicationCount:chain.applications,activeSession:Boolean(activeSession),
  waitingForUser:ctx?.status==='WAITING_FOR_USER',hasSelectedJob}):null;
 // F07Z-145: the review and the workbench share this. No job yet → research first (with the
 // intent remembered) instead of a form that would refuse the submission.
 const openApplicationEntry=()=>{
  if(!hasSelectedJob){setApplicationIntent(true);setPane('research');return;}
  setPane('apps');
 };
 const prepNext=(()=>{
  if(!prepState)return null;
  if(prepState.status==='PENDING'||prepState.status==='RUNNING')
   return {kind:'busy' as const,label:'正在准备…',description:'本机正在生成训练计划；完成后会自动显示，不需要手动刷新。'};
  if(prepState.status==='WAITING_FOR_USER')
   return {kind:'confirm' as const,label:'开始这场',description:'计划已冻结；确认后只创建一场面试。'};
  if(['FAILED','STALE','REJECTED','CANCELLED'].includes(prepState.status))
   return {kind:'retry' as const,label:'重新准备',description:`上次准备未完成${prepState.error_code?`（${prepState.error_code}）`:''}；可以重新生成计划。`};
  if(prepState.status==='SUCCEEDED'&&prepState.session_id){
   const session=ctx?.history.find(item=>item.id===prepState.session_id);
   if(!session||!['COMPLETED','FAILED','CANCELLED'].includes(session.status))
    return {kind:'open' as const,label:'继续本场',sessionId:prepState.session_id,
     description:'准备已确认，这场面试已创建；打开继续作答，不会重复开场。'};
  }
  return null;
 })();
 const workbenchNext=prepNext
  ?{label:prepNext.label,description:prepNext.description}
  :(progress?{label:progress.nextAction.label,description:progress.nextAction.description}:null);
 const runNextAction=()=>{
  if(prepNext){
   if(prepNext.kind==='busy')return;
   if(prepNext.kind==='confirm'){window.dispatchEvent(new CustomEvent('py-prep-confirm'));return;}
   if(prepNext.kind==='retry'){window.dispatchEvent(new CustomEvent('py-prep-retry'));return;}
   if(prepNext.kind==='open'){openSessionAt(prepNext.sessionId);return;}
  }
  if(!progress)return;
  switch(progress.nextAction.kind){
   case 'create-goal':setPane('workbench');setFormOpen(true);window.setTimeout(()=>formRef.current?.focus(),0);break;
   case 'add-materials':case 'continue-resume':launchAgent('resume');break;
   case 'start-interview':case 'continue-interview':primaryAction();break;
   case 'view-review':{if(finished.length)openSessionAt(finished[0].id);else setPane('interview');break;}
   case 'record-application':openApplicationEntry();break;
  }
 };
 // One line, one place: progress · clock-or-terminal-state · status · session.
 // The status word is dropped when it already is the terminal state ("已结束 · 已结束").
 // Figures are wrapped so every number in the shell uses the same face and weight.
 const num=(text:string|number)=><span className="d-num">{text}</span>;
 const interviewLine=(()=>{
  if(!ctx?.sessionId)return pane==='interview'?'尚未开场':'';
  const index=Math.min(Math.max(ctx.questionIndex,1),ctx.maxQuestions||ctx.questionIndex);
  const status=statusLabel[ctx.status??'']??ctx.status;
  return <>
   第 {num(`${index}/${ctx.maxQuestions}`)}{' 题 · '}
   {ctx.terminal?status:<>{'剩余 '}{num(formatRemaining(remaining))}</>}
   {!ctx.terminal&&status?` · ${status}`:''}
   {' · 场次 '}{num(ctx.sessionId.slice(0,8))}
  </>;
 })();
 // F07Z-77: a short handle for the current account. The local part of the address is what
 // actually differs between accounts; the domain is noise in a 200px rail.
 const accountTag=accountEmail?accountEmail.split('@')[0].slice(0,12):'';
 // F07Z-88: where this machine keeps its configuration and logs. The host reports the paths and
 // whether each config file exists; the page can ask it to open one of those two folders, and
 // nothing else. A browser tab has no bridge, so the section explains instead of guessing.
 type LocalData={layout:string;config_dir:string;run_data_dir:string;
  files:{name:string;present:boolean}[];config_from_env:boolean};
 const [localData,setLocalData]=useState<LocalData|null>(null);
 useEffect(()=>{
  const core=hostCore();
  if(!core)return;
  let alive=true;
  void core.invoke<LocalData>('local_data').then(value=>{if(alive)setLocalData(value);}).catch(()=>{});
  return()=>{alive=false;};
 },[]);
 async function openDataFolder(kind:'config'|'run'){
  const core=hostCore();
  if(!core)return;
  try{await core.invoke('open_data_folder',{kind});}
  catch(e){setError(`打不开本地目录：${String(e)}`);}
 }
 const pageLabel=pane==='workbench'?'工作台':pane==='artifacts'?'产物':pane==='interview'?'面试':pane==='written'?'笔试':pane==='research'?'岗位研究':pane==='materials'?'材料与简历':pane==='apps'?'申请跟踪':pane==='banks'?'题库':'设置';

 return <div className="desktop-shell" data-ui-variant="B" data-theme={appearance} data-pane={pane} data-answering={answering?'true':'false'} data-rail={railOpen?'open':'collapsed'}>
  <header className="desktop-topbar" {...dragSurfaceProps()}>
   {/* The product window is undecorated, so the top bar is what moves the window.
       The whole bar accepts the press; buttons, the omnibox and menus opt out. */}
   <button type="button" className="d-rail-toggle" data-testid="rail-toggle" title={railOpen?'收起侧栏':'展开侧栏'}
    aria-expanded={railOpen} aria-controls="desktop-rail" onClick={toggleRail}>
    <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4" aria-hidden="true"><rect x="2" y="2.8" width="12" height="10.4" rx="1.2"/><path d="M6.2 2.8v10.4"/></svg>
   </button>
   <span className="d-topbar-title">求职工作台</span>
   {/* The parallel-version badge is a development affordance only. In the frozen
       product there is no second version and no Vite port, so it must not ship. */}
   {import.meta.env.DEV&&<span className="d-ui-variant" data-testid="ui-variant-b"
    title="与 5174 端口的版本 A 并行运行">版本 B · 求职软件 + 三 Agent</span>}
   <span className="d-topbar-gap"/>
   <form className="d-topbar-omni" data-testid="agent-omnibox" onSubmit={event=>void submit(event)}
    aria-label="一句话交给 Agent">
    <input ref={omniRef} aria-label="一句话交给 Agent" value={message} maxLength={2000}
     onChange={event=>setMessage(event.target.value)} placeholder="一句话交给 Agent，例如：帮我把项目经历改成简历并练一场…"/>
    <button type="submit" className="d-omni-send" data-testid="agent-omnibox-send" aria-label="发送这一句"
     title="发送这一句给本机协调器" disabled={busy||(!message.trim()&&!jd.trim()&&!direction.trim())}>↵</button>
   </form>
   <div className="d-topbar-right">
    <div className="d-theme-quick" role="group" aria-label="外观快捷切换">
     {([['light','浅色','☀'],['dark','深色','☾'],['auto','跟随系统','◐']] as const).map(([key,label,icon])=>
      <button key={key} type="button" title={label} aria-label={label} aria-pressed={appearanceChoice===key}
       onClick={()=>onAppearanceChange?.(key)}>{icon}</button>)}
    </div>
        {/* F07Z-77: the summary names the account, not just "账号": the local identity and a
        signed-in account are different data sets, and the rail should say which one is open. */}
    <details className="d-account"><summary aria-label="账号菜单" data-testid="account-summary"
     title={accountEmail??undefined}>{accountName??'账号'}{accountTag&&<span className="d-account-tag"> · {accountTag}</span>} ▾</summary>
     <div className="d-account-menu"><p>{accountName??'本机身份'}</p>
      {accountEmail&&<p className="fine-print">{accountEmail}</p>}
      {onSwitchAccount&&<button type="button" className="text-button" onClick={onSwitchAccount}>切换账号</button>}
      {onLogout&&<button type="button" className="text-button" onClick={onLogout}>退出登录</button>}</div></details>
    {/* The native title bar is gone, so the app provides these itself. They only
        appear inside the desktop window; a browser tab has no window to control. */}
    {windowBridge&&<div className="d-window-controls" role="group" aria-label="窗口控制">
     <button type="button" data-testid="win-minimize" title="最小化" aria-label="最小化"
      onClick={()=>void windowAction('minimize')}>─</button>
     <button type="button" data-testid="win-maximize" data-maximized={maximized?'true':'false'}
      title={maximized?'还原窗口':'最大化'} aria-label={maximized?'还原窗口':'最大化'}
      aria-pressed={maximized}
      onClick={()=>void windowAction('toggle_maximize')}>{maximized?'❐':'▢'}</button>
     <button type="button" className="is-close" data-testid="win-close" title="关闭" aria-label="关闭"
      onClick={()=>void windowAction('close')}>✕</button>
    </div>}
   </div>
  </header>
  <div className="desktop-work">
  <aside className="desktop-rail" id="desktop-rail" aria-label="工作区导航" {...railCaptionProps()}>
   {/* No brand block: the top bar already carries the product name, and the rail is
       the app's outermost edge. A second title here only repeated it. */}
   {/* F07Z-72: this block only repeated the sidebar entries - clicking 面试 Agent landed
       on the same 面试 pane the rail already has. Removed per user decision; the sidebar is
       the entry, and the workbench keeps its role cards with state. */}
   <nav className="d-nav" aria-label="主导航">
    <div className="d-nav-label">工作</div>
    <button type="button" className="d-nav-item" data-testid="agent-home" title="工作台" aria-current={pane==='workbench'?'page':undefined} onClick={()=>setPane('workbench')}>
     <span aria-hidden="true"><svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4"><rect x="2" y="2" width="5" height="5" rx="1"/><rect x="9" y="2" width="5" height="5" rx="1"/><rect x="2" y="9" width="5" height="5" rx="1"/><rect x="9" y="9" width="5" height="5" rx="1"/></svg></span>
     <span className="d-nav-label">工作台</span></button>
    <button type="button" className="d-nav-item" title="面试" aria-current={pane==='interview'?'page':undefined}
     onClick={()=>setPane('interview')}>
     <span aria-hidden="true"><svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4"><path d="M3 3.5h10M3 8h10M3 12.5h6"/></svg></span>
     <span className="d-nav-label">面试</span>{signals.badges.interviews&&<span className="d-nav-badge" data-testid="nav-badge-interviews">{signals.badges.interviews}</span>}</button>
    <button type="button" className="d-nav-item" title="笔试" aria-current={pane==='written'?'page':undefined} onClick={()=>setPane('written')}>
     <span aria-hidden="true"><svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4"><path d="M3 2.8h10v10.4H3z"/><path d="M5.2 6h5.6M5.2 8.4h5.6M5.2 10.8h3.2"/></svg></span>
     <span className="d-nav-label">笔试</span>{signals.badges.written&&<span className="d-nav-badge" data-testid="nav-badge-written">{signals.badges.written}</span>}</button>
    <button type="button" className="d-nav-item" title="岗位研究" aria-current={pane==='research'?'page':undefined} onClick={()=>setPane('research')}>
     <span aria-hidden="true"><svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4"><circle cx="8" cy="8" r="5.5"/><path d="M8 4.8v3.6l2.2 1.4"/></svg></span>
     <span className="d-nav-label">岗位研究</span>{signals.badges.research&&<span className="d-nav-badge" data-testid="nav-badge-research">{signals.badges.research}</span>}</button>
     {/* F07Z-81: 产物 is a pane like the rest, so it belongs in the rail. It used to be
         reachable only from the top bar, which meant one navigation surface had an entry
         the other navigation surface did not. */}
     <button type="button" className="d-nav-item" data-testid="nav-artifacts" title="Agent 产物"
      aria-current={pane==='artifacts'?'page':undefined} onClick={()=>setPane('artifacts')}>
      <span aria-hidden="true"><svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4"><path d="M3.4 2.6h6L13 6.2v7.2H3.4z"/><path d="M9.4 2.6v3.6h3.6"/><path d="M5.6 9.4h4.8M5.6 11.6h3.2"/></svg></span>
      <span className="d-nav-label">产物</span></button>
    <div className="d-nav-label">材料</div>
    <button type="button" className="d-nav-item" title="材料与简历" aria-current={pane==='materials'?'page':undefined}
     onClick={()=>{setResumeFocus(false);setPane('materials');}}>
     <span aria-hidden="true"><svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4"><path d="M4 2.5h5.5L13 6v7.5H4z"/><path d="M9 2.5V6h4"/></svg></span>
     <span className="d-nav-label">材料与简历</span>{signals.badges.materials&&<span className="d-nav-badge" data-testid="nav-badge-materials">{signals.badges.materials}</span>}</button>
    <button type="button" className="d-nav-item" title="申请跟踪" aria-current={pane==='apps'?'page':undefined} onClick={()=>setPane('apps')}>
     {/* A footprint. At 15px a thin sole outline collapses into a "0", so this is the
         filled form: one pad plus three toes, which stays readable at rail size. */}
     <span aria-hidden="true"><svg viewBox="0 0 16 16" fill="currentColor"><ellipse cx="8" cy="10.6" rx="3.3" ry="3.7"/><circle cx="5.3" cy="5.3" r="1.45"/><circle cx="8" cy="4.2" r="1.45"/><circle cx="10.7" cy="5.3" r="1.45"/></svg></span>
     <span className="d-nav-label">申请跟踪</span>{signals.badges.apps&&<span className="d-nav-badge" data-testid="nav-badge-apps">{signals.badges.apps}</span>}</button>
    <div className="d-nav-label">应用</div>
    <button type="button" className="d-nav-item" title="题库" aria-current={pane==='banks'?'page':undefined} onClick={()=>setPane('banks')}>
     <span aria-hidden="true"><svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4"><path d="M3 2.8h10v10.4H3z"/><path d="M5.4 5.6h5.2M5.4 8h5.2M5.4 10.4h3"/></svg></span>
     <span className="d-nav-label">题库</span></button>
    <button type="button" className="d-nav-item" data-testid="nav-settings" title="设置" aria-current={pane==='settings'?'page':undefined} onClick={()=>setPane('settings')}>
     {/* A gear, not a sun: the previous icon was a circle with eight rays, which
         reads as brightness rather than settings. */}
     <span aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.5 14.6a1.6 1.6 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.6 1.6 0 0 0-1.8-.3 1.6 1.6 0 0 0-1 1.5v.2a2 2 0 1 1-4 0v-.1a1.6 1.6 0 0 0-1-1.5 1.6 1.6 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.6 1.6 0 0 0 .3-1.8 1.6 1.6 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.6 1.6 0 0 0 1.5-1 1.6 1.6 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.6 1.6 0 0 0 1.8.3H9a1.6 1.6 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.6 1.6 0 0 0 1 1.5 1.6 1.6 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.6 1.6 0 0 0-.3 1.8V9a1.6 1.6 0 0 0 1.5 1h.2a2 2 0 1 1 0 4h-.1a1.6 1.6 0 0 0-1.5 1z"/></svg></span>
     <span className="d-nav-label">设置</span></button>
   </nav>
   <div className="d-rail-foot">
    <p>目标 {goal?goal.id.slice(0,8):'—'}</p>
    <p>{ids.length} 个求职目标</p>
   </div>
  </aside>
  <div className="desktop-main">
   <header className="desktop-context">
    <div className="d-context-goal" data-goal-state={goal?'ready':goalLoading?'loading':goalRoutePending?'error':'empty'}>
     <strong data-testid="desktop-goal">{goal?(goal.conditions.direction||'已保存的求职目标')
      :goalRoutePending?(goalLoading?'正在读取目标':'目标没有读出来'):'尚未创建求职目标'}</strong>
     <small>{goal?(phases[goal.status]??goal.status)
      :goalRoutePending?(goalLoading?'链接里的目标读取中':'读取失败，可在工作台重试'):'先写一句求职意向'}</small></div>
    {/* The desktop shell re-reads the goal on a timer, but a user who just hit an
        error needs an explicit way to pull the current state themselves. */}
    {goal&&<button type="button" className="text-button d-context-refresh" data-testid="desktop-refresh"
     disabled={busy} onClick={()=>void refresh()}>刷新目标</button>}
    <span className="d-divider" aria-hidden="true"/>
    <div className="d-context-page" data-testid="desktop-page">{pageLabel}</div>
    {pane==='interview'&&<div className="d-context-session" data-testid="desktop-session">
     {interviewLine}</div>}
    {pane!=='workbench'&&selection&&<div className="d-context-pick" data-testid="desktop-selection"
     title={`${selection.title}${selection.company?` · ${selection.company}`:''}`}>已选 {selection.title}</div>}
    <span className="d-spacer"/>
   </header>
   <main className="desktop-body">
    <div className="desktop-pane" hidden={pane!=='workbench'}>
     {/* F07Z-81: this was a top-bar button that switched panes and scrolled. The target is
         part of this page, so the jump belongs here - and it opens the section if collapsed. */}
     <div className="d-pane-head d-pane-head-actions"><h1>{goal?'求职工作台'
      :goalLoading?'正在读取目标':goalRoutePending?'目标没有读出来':'先从一句求职意向开始'}</h1>
      <button type="button" className="text-button" data-testid="agent-rail-jump"
       onClick={()=>{const el=document.getElementById('agent-rail') as HTMLDetailsElement|null;
        if(!el)return;el.open=true;el.scrollIntoView({block:'start',behavior:'smooth'});}}>查看运行详情</button>
     </div>
     {/* F07Z-141: one surface, one primary. The old separate "创建求职目标" CTA card and the
         form card were two entries for the same task; they are now a single card whose
         submit is the primary action, so the first screen has nothing to choose between. */}
     <section className={`card d-card${goal||goalRoutePending?'':' d-empty-cta'}${goalRoutePending?' d-goal-route':''}`}
      aria-label={goal?'当前求职目标':goalRoutePending?(goalLoading?'正在读取目标':'目标读取失败'):'创建求职目标'}
      data-testid={goal||goalRoutePending?undefined:'empty-create-cta'}>
      {goalRoutePending?goalRouteNotice:<>
      <div className="d-goal-row">
       <div>{goal&&<span className="eyebrow d-goal-eyebrow">当前目标</span>}
        <h2 data-testid="workbench-goal">{goal?goalView.title:'创建求职目标'}</h2>
        <p className="muted">{goal?goalView.meta:'告诉我你想找什么工作。方向还不清楚也可以开始，简历不是前置条件。'}</p>
        {goalView.detail&&<p className="fine-print d-goal-detail" data-testid="workbench-goal-detail">{goalView.detail}</p>}</div>
       {goal&&<button className="secondary" data-testid="workbench-primary" type="button"
        disabled={busy||goal.status==='CANCELLED'} onClick={primaryAction}>{primaryLabel}</button>}
      </div>
      {goal&&<p className="fine-print">{provenance}</p>}
      {goal&&goalJustCreated&&progress?.currentStage==='materials'&&
       <p className="notice" data-testid="goal-created-next">{getAsyncActionCopy('goal-create','success').feedbackTitle}。{getAsyncActionCopy('goal-create','success').feedbackBody}</p>}
      {error&&<p role="alert" className="error">{error}</p>}
      {goal&&<details className="d-goal-details" open={formOpen} onToggle={e=>{if(e.target===e.currentTarget)setFormOpen((e.target as HTMLDetailsElement).open);}}>
       <summary>调整目标条件</summary>{goalForm()}</details>}
      {!goal&&<div className="d-goal-form">{goalForm(false)}
       <div className="d-form-foot"><span className="muted">保存后由 Agent 接手；已经有 JD 时也可以直接准备一场。</span>
        <button className="text-button" data-testid="workbench-train" type="submit" form="desktop-goal-form"
         disabled={busy||(!message.trim()&&!jd.trim()&&!direction.trim())}
         onClick={()=>{submitMode.current='training';}}>直接准备一场面试</button>
        <button className="primary" data-testid="workbench-primary" data-workbench-primary-action type="submit" form="desktop-goal-form"
         disabled={busy||(!message.trim()&&!jd.trim()&&!direction.trim())}
         onClick={()=>{submitMode.current='save';}}>{busy
          ?getAsyncActionCopy('goal-create','submitting').buttonLabel
          :<span data-testid="empty-create-goal">{primaryLabel}</span>}</button></div></div>}
      </>}
     </section>
      {goal&&progress&&<nav className="d-progress" aria-label="求职进度" title={progress.summary} data-testid="career-progress">
       {progress.stages.map(stage=><span key={stage.key} className={`d-progress-step d-progress-${stage.state}`}
        data-stage={stage.key} data-state={stage.state}
        aria-current={stage.state==='current'?'step':undefined}>
        <span className="d-progress-mark" aria-hidden="true">{stage.state==='completed'?'✓':stage.state==='current'?'●':stage.state==='attention'?'!':'○'}</span>{stage.label}</span>)}
      </nav>}
      {goal&&progress&&shouldExplainOptionalStages(progress)&&
       <p className="fine-print d-progress-hint" data-testid="progress-optional-hint">{OPTIONAL_STAGE_HINT}</p>}
      {!goalRoutePending&&<AgentPipeline goal={goal} onOpenPane={next=>setPane(next)}
       next={workbenchNext}
       nextDisabled={prepNext?.kind==='busy'}
       nextUseResume={!noResume}
       onNext={runNextAction} onOpenSession={id=>openSessionAt(id)}/>}
     {!goalRoutePending&&<AgentDock goal={goal} onLaunch={launchAgent}/>}
     {!goalRoutePending&&<div className="d-workbench">
      <div className="d-workbench-main">
      <section className="card d-card" aria-label="近期场次">
       <div className="d-card-head"><h2>当前目标的近期场次</h2>
        <button type="button" className="text-button" onClick={()=>setPane('interview')}>打开面试</button></div>
       {!goal&&<p className="muted">保存求职目标后，这里会列出本目标的整场面试记录。</p>}
       {goal&&(!ctx||ctx.history.length===0)&&<p className="muted">还没有整场面试记录。</p>}
       {goal&&ctx&&ctx.history.length>0&&<div className="d-session-list">
        {ctx.history.slice(0,5).map((item,index)=><button key={item.id} type="button" className="d-session-item"
         onClick={()=>openSessionAt(item.id)}>
         <span><strong>场次 {ctx.history.length-index} · {item.id.slice(0,8)}</strong>
          <small>{item.mode==='coaching'?'陪练':'实战'} · {item.bank} · {item.questions} 题 / {item.answers} 答</small></span>
         <span className={`tag ${item.status==='COMPLETED'?'done':item.status==='FAILED'?'bad':''}`}>{statusLabel[item.status]??item.status}</span>
        </button>)}
       </div>}
      </section>
      <details className="d-coordinator-details" id="agent-rail" open>
       <summary className="d-section-label">运行详情：下一步建议由本机协调器给出</summary>
       <AgentConsole goal={goal} onOpenPane={openAgentPane} onFocusInput={focusAgentInput}
        trainingTask={Boolean(prepState&&['PENDING','RUNNING','WAITING_FOR_USER','SUCCEEDED'].includes(prepState.status))}
        onOpenTraining={()=>{
         const el=document.querySelector('[data-testid="workbench-next-action"]') as HTMLElement|null;
         el?.scrollIntoView({block:'center',behavior:'smooth'});}}
        onWorkflow={setAgentFlow} onChanged={()=>void refresh()}/>
      </details>
     {goal&&<details className="card d-card d-zone" id="d-raw"><summary>原始输入记录</summary>
      {goal.steps.map(step=><article className="career-message" key={step.id}><p style={{whiteSpace:'pre-wrap'}}>{step.message}</p><small>{phases[step.status]??step.status}</small></article>)}</details>}
      </div>
      <aside className="d-workbench-side" aria-label="进行中与快捷入口">
       <section className="card d-card">
        <div className="d-card-head"><h2>进行中的场次</h2></div>
        {activeSession?<>
         <p className="muted">场次 {activeSession.id.slice(0,8)} · 已出 {activeSession.questions} 题 / {activeSession.answers} 答</p>
         <button type="button" className="secondary d-side-action" onClick={primaryAction}>继续这场<span aria-hidden="true">→</span></button>
        </>:<p className="muted">{goal?'当前没有等你回答的场次。':'先保存求职目标，才能开始整场面试。'}</p>}
       </section>
       {ctx&&<section className="card d-card">
        <div className="d-card-head"><h2>累计练习</h2></div>
        <div className="d-side-stats">
         <div><b className="d-num d-num-mid">{ctx.history.length}</b><span>场次</span></div>
         <div><b className="d-num d-num-mid">{ctx.history.reduce((sum,item)=>sum+item.answers,0)}</b><span>答题</span></div>
         <div><b className="d-num d-num-mid">{ctx.history.filter(item=>item.status==='COMPLETED').length}</b><span>报告</span></div>
        </div>
       </section>}
       {goals.length>1&&<section className="card d-card">
        <div className="d-card-head"><h2>其他目标</h2><button type="button" className="text-button" disabled={busy} onClick={newGoal}>新建</button></div>
        <div className="d-goal-list">
         {goals.filter(item=>item.id!==goal?.id).slice(0,4).map(item=>
          <button key={item.id} type="button" className="d-goal-item" title={item.direction||'未命名目标'} onClick={()=>void choose(item.id)}>
           <span>{item.direction||'未命名目标'}</span>
           <small>{phases[item.status]??item.status}</small>
          </button>)}
        </div>
       </section>}
       <AgentArtifacts workflow={agentFlow} onOpenPane={openAgentPane} onNewGoal={newGoal} compact/>
      </aside>
     </div>}
    </div>
    <div className="desktop-pane" hidden={pane!=='artifacts'} data-testid="artifacts-pane">
     <div className="d-pane-head"><h1>Agent 产物</h1><p>两个 Agent 与协调器已经跑出来的结果，按「运行结果 / 记录与配置」分区。</p></div>
     <AgentArtifacts workflow={agentFlow} onOpenPane={openAgentPane} onNewGoal={newGoal}/>
    </div>
    <div className="desktop-pane" hidden={pane!=='interview'}>
     {goal?<InterviewPanel key={goal.id} variant="desktop" userId={userId} goalId={goal.id} goalVersion={goal.version} goalStatus={goal.status}
      assessmentMode={assessmentMode} agentMode={agentMode} onAgentModeChange={setAgentMode} onAuthLost={onAuthLost} onGoalRefresh={()=>void refresh()} onContext={receiveContext}
      openSessionId={openSessionId} onOpenHandled={()=>setOpenSessionId(null)} onOpenApplications={openApplicationEntry}
      onOpenSession={openSessionAt}
      goalTitle={goalView.title} hasSelectedJob={hasSelectedJob} jobKeyPoints={jobKeyPoints}/>
      :<GoalNeeded title="面试需要先有求职目标"
        note="整场面试挂在当前求职目标下。先在工作台保存一句求职意向，再回来开场。"
        goals={goals} busy={busy} onChoose={choose} onCreate={()=>setPane('workbench')}/>}
    </div>
    <div className="desktop-pane" hidden={pane!=='research'} data-testid="research-pane">
     {goal?<JobResearchPanel key={`${goal.id}:${goal.version}`} userId={userId} goalId={goal.id} version={goal.version}
       jdText={goal.jd_text} direction={goal.conditions.direction} cancelled={goal.status==='CANCELLED'}
       goalStatus={goal.status} automatic={goal.conditions.auto_research??false}
       onGoalRefresh={()=>void refresh()} onAuthLost={onAuthLost} onSelection={setSelection}
       applicationIntent={applicationIntent} onOpenApplications={()=>{setApplicationIntent(false);setPane('apps');}}/>
      :<GoalNeeded title="岗位研究需要先有求职目标"
        note="研究与选岗挂在当前求职目标下。先在工作台保存一句求职意向，再回来读取岗位。"
        goals={goals} busy={busy} onChoose={choose} onCreate={()=>setPane('workbench')}/>}
    </div>
    <div className="desktop-pane" hidden={pane!=='materials'} data-testid="materials-pane">
     {goal?<MaterialsPanel key={`${goal.id}:${goal.version}`} goalId={goal.id} version={goal.version}
       goalStatus={goal.status} cancelled={goal.status==='CANCELLED'} resumeFocus={resumeFocus}
       onAuthLost={onAuthLost} onGoalRefresh={()=>void refresh()} onStartInterview={()=>setPane('interview')}
      goalTitle={goalView.title} jobKeyPoints={jobKeyPoints}/>
      :<GoalNeeded title="材料与简历需要先有求职目标"
        note="材料挂在当前求职目标下。先在工作台保存一句求职意向，再回来整理简历。"
        goals={goals} busy={busy} onChoose={choose} onCreate={()=>setPane('workbench')}/>}
    </div>
    <div className="desktop-pane" hidden={pane!=='apps'} data-testid="apps-pane">
     {goal?<ApplicationsPanel key={`${goal.id}:${goal.version}`} goalId={goal.id} version={goal.version}
       cancelled={goal.status==='CANCELLED'} active={pane==='apps'} onAuthLost={onAuthLost} onGoalRefresh={()=>void refresh()}
      goalTitle={goalView.title} onOpenResearch={()=>{setApplicationIntent(true);setPane('research');}}/>
      :<GoalNeeded title="申请跟踪需要先有求职目标"
        note="投递记录挂在当前求职目标下。先在工作台保存一句求职意向，再回来记录。"
        goals={goals} busy={busy} onChoose={choose} onCreate={()=>setPane('workbench')}/>}
    </div>
    <div className="desktop-pane" hidden={pane!=='written'} data-testid="written-pane">
     {goal?<>
       <WrittenPanel key={`${goal.id}:${goal.version}`} goalId={goal.id} version={goal.version}
        cancelled={goal.status==='CANCELLED'} onAuthLost={onAuthLost} onGoalRefresh={()=>void refresh()}
        openRequest={writtenRequest}/>
       <AlgorithmPracticePanel goalId={goal.id} onAuthLost={onAuthLost}/>
      </>
      :<GoalNeeded title="笔试需要先有求职目标"
        note="笔试记录挂在当前求职目标下。先在工作台保存一句求职意向，再回来开始。"
        goals={goals} busy={busy} onChoose={choose} onCreate={()=>setPane('workbench')}/>}
    </div>
    <div className="desktop-pane" hidden={pane!=='banks'} data-testid="banks-pane">
     <QuestionBanksPanel onAuthLost={onAuthLost}
      goalId={goal?.id} goalVersion={goal?.version}
      onStarted={id=>{setWrittenRequest(id);setPane('written');}}/>
    </div>
    <div className="desktop-pane" hidden={pane!=='settings'} data-testid="settings-pane">
     <div className="d-pane-head"><h1>设置</h1><p>外观与本地数据。改动即时生效，关掉窗口再打开也保留。</p></div>
     <div className="d-settings">
      <section className="d-set-group">
       <h2>外观</h2>
       <p className="hint">只影响这台机器上的桌面窗口，不影响浏览器里打开的页面。</p>
       <div className="d-themes" role="group" aria-label="外观">
        {([['light','浅色'],['dark','深色'],['auto','跟随系统']] as const).map(([key,label])=>
         <button key={key} type="button" className="d-theme" data-preview={key} data-testid={`theme-${key}`}
          aria-pressed={appearanceChoice===key} onClick={()=>onAppearanceChange?.(key)}>
          <div className="pv" aria-hidden="true">
           <div className="a"><i/><i/><i/></div>
           <div className="b"><i/><i/><i/></div>
          </div>
          <div className="lbl">{label}
           <svg className="tick" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true"><path d="M3 8.4l3.2 3.2L13 5"/></svg></div>
         </button>)}
       </div>
      </section>
      <section className="d-set-group">
       <h2>账号</h2>
       <p className="hint">首次启动会自动创建一个本机身份；它的练习历史只存在本机服务里。登录其他账号可以读取那边的记录。</p>
       <div className="d-set-row"><span>当前身份</span><span className="val">{accountName??'本机身份'}</span></div>
       {accountEmail&&<div className="d-set-row"><span>标识</span><span className="val d-num d-num-sm">{accountEmail}</span></div>}
       {onSwitchAccount&&<div className="d-set-actions"><button type="button" className="secondary" onClick={onSwitchAccount}>切换账号</button></div>}
      </section>
      <AgentPolicySection/>
      <SkillsSection/>
      <StrategiesSection/>
      <ConnectorsSection/>
      <InterviewWikiSection/>
      <ModelSettingsSection/>
      <section className="d-set-group" data-testid="local-data">
       <h2>本机数据</h2>
       <p className="hint">练习记录与草稿保存在本机服务里，不上传到外部。配置文件与日志都在下面这两个目录里。</p>
       <div className="d-set-row"><span>数据库与缓存</span><span className="val">本机服务</span></div>
       {localData
        ?<>
         <div className="d-set-row"><span>安装形态</span><span className="val">{localData.layout==='installed'?'安装版（配置在用户目录）':'文件夹版（配置在程序旁边）'}</span></div>
         <div className="d-set-row"><span>配置目录</span><span className="val d-num d-num-sm" data-testid="config-dir">{localData.config_dir}</span></div>
         <div className="d-set-row"><span>日志与状态</span><span className="val d-num d-num-sm">{localData.run_data_dir}</span></div>
         <div className="d-set-row"><span>配置文件</span><span className="val">
          {localData.files.map(item=><span key={item.name} className={`tag ${item.present?'done':'bad'}`}
            data-testid={`config-file-${item.name}`}>{item.name.replace('.json','')}{item.present?'':' 缺失'}</span>)}
         </span></div>
         {localData.files.some(item=>!item.present)&&
          <p className="fine-print">缺文件时本地服务不会启动；把对应的 json 放进上面的配置目录，再重新打开程序。</p>}
         <div className="d-set-actions">
          <button type="button" className="secondary" onClick={()=>void openDataFolder('config')}>打开配置目录</button>
          <button type="button" className="text-button" onClick={()=>void openDataFolder('run')}>打开日志目录</button>
         </div>
        </>
        :<p className="fine-print">浏览器里读不到本机目录；桌面版会在这里给出配置目录与日志目录，并能直接打开。</p>}
      </section>
      <section className="d-set-group" data-testid="about-version">
       <h2>关于本版本</h2>
       <div className="d-set-row"><span>版本</span><span className="val d-num">{__APP_VERSION__} · 预览版</span></div>
       {/* Two facts only: what this build is, and what it will not do for you.
           The API routes, the frozen request params and the per-step execution
           records were internal engineering detail; they are documented in the
           hand-off report instead of being shown to whoever uses the app. */}
       <div data-testid="about-mode">
        <div className="d-set-row"><span>运行方式</span><span className="val">本机内置脚本，离线运行</span></div>
        <p className="fine-print">脚本只演示对话流程，不代表真实语义理解；未调用付费/live 模型，额度读数未知。</p>
       </div>
       <div data-testid="about-boundary">
        <p className="fine-print">面试需要你本人作答，不会代答；一键准备的第三步是系统汇总，不是第三个 Agent；材料改写需要至少一段原文，产出是初稿。</p>
       </div>
      </section>
     </div>
    </div>
   </main>
   <div className="desktop-status" data-testid="desktop-status">
    <span className="dot" aria-hidden="true"/><span>本地工作台</span>
    <span className="sep" aria-hidden="true">·</span><span>数据保存在本机</span>
    <span className="d-spacer"/>
    <span className="d-num">v{__APP_VERSION__}</span>
   </div>
  </div>
 </div>
 </div>;
}
