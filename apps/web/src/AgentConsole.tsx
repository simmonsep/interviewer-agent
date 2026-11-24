import {useCallback,useEffect,useRef,useState} from 'react';
import {api,post,describeError,ApiError} from './api.ts';

// The desktop shell used to expose the backend as eight hand-operated CRUD panes.
// This console puts the existing Coordinator Agent back in charge: one sentence in,
// the agent picks the next step, proposals are executed with one click, and every
// run leaves a visible trace. Nothing here pretends the free scripted coordinator
// is semantic intent recognition.
//
// F06B adds the autonomy policy from the open-source agents we studied: goose's
// modes, Codex's approval policies and opencode's once/always/reject decisions.
// The server is authoritative for the effect; this component only renders it.

export interface AgentStep{
 id:string;goal_version:number;message:string;status:string;error_code:string|null;
 origin:string|null;model_requests:number;tool_log:{tool:string;status:string}[]|null;
}
export interface AgentGoal{
 id:string;version:number;status:string;
 conditions:{direction:string;cities:string[];auto_research?:boolean};
 action:{type:string;text:string}|null;
 profile:{direction:{value:string}|null;cities:{value:string}[];recruitment:{value:string}|null}|null;
 steps:AgentStep[];
}
export interface AgentWorkflow{
 goal_version:number;
 selected_job:{snapshot_id:string;title:string}|null;
 materials:{source_count:number;confirmed_fact_count:number;
  runs:{id:string;status:string;result_action:string|null}[]};
 interviews:{id:string;status:string}[];
 written_tests:{id:string;status:string}[];
 observations:{id:string}[];
}
export interface AgentPolicy{
 mode:string;grants:Record<string,string>;effects:Record<string,string>;
 modes:{id:string;label:string;description:string}[];note:string;
}
export type AgentPane='interview'|'written'|'research'|'materials'|'apps'|'banks';

const phases:Record<string,string>={PENDING:'等待协调',RUNNING:'正在梳理目标',WAITING_FOR_USER:'等待你补充',
 WAITING_FOR_TOOL:'下一步请求已准备',WAITING_CREDENTIALS:'模型调用已暂停',FAILED:'本次协调未完成',
 CANCELLED:'已结束',COMPLETED:'本轮已完成',SUCCEEDED:'已处理'};
const toolText:Record<string,string>={read_goal:'读取目标',read_application:'读取申请',
 ask_user:'提出澄清',prepare_job_research:'准备岗位研究',prepare_training:'准备训练',
 prepare_materials:'准备材料任务',prepare_application_action:'准备申请动作',finish:'结束本轮'};
const proposals:Record<string,{title:string;cta:string;pane?:AgentPane}>={
 ask_user:{title:'需要你补充一点信息',cta:'去补充'},
 prepare_job_research:{title:'下一步：岗位研究',cta:'交给 Agent 执行'},
 prepare_training:{title:'下一步：定向面试训练',cta:'执行训练准备'},
 prepare_materials:{title:'下一步：材料分析 / 简历草稿',cta:'执行材料准备'},
 prepare_application_action:{title:'下一步：申请复盘动作',cta:'打开申请',pane:'apps'},
 finish:{title:'本轮已结束',cta:''},
};
const riskText:Record<string,string>={low:'低风险 · 只读/研究',high:'高风险 · 会写入本机记录'};

function failure(error:unknown):string{
 if(error instanceof ApiError){
  if(error.code==='coordinator_proposal_changed')return 'Agent 这一轮的提议已变化，请等它跑完再确认，避免把旧提议写进记录。';
  if(error.code==='handoff_already_confirmed')return '这条提议已经确认执行过了，不会重复创建。';
  if(error.code==='research_in_progress')return '这一轮已经有一个岗位研究任务在排队，Agent 不会重复提交；等它产出结果或开始新一轮后再试。';
  if(error.code==='version_conflict')return '目标状态已经变化，请等 Agent 读完最新一轮再执行。';
  if(error.code==='idempotency_conflict')return '这条提议已经提交过，系统拒绝用相同凭据重复提交。';
  if(error.serverMessage)return error.serverMessage;
 }
 return describeError(error);
}

export function AgentConsole({goal,onOpenPane,onFocusInput,onWorkflow,onChanged,trainingTask,onOpenTraining}:{
 goal:AgentGoal|null;onOpenPane:(pane:AgentPane)=>void;onFocusInput:()=>void;
 onWorkflow:(value:AgentWorkflow|null)=>void;onChanged:()=>void;
 /** F07Z-166: the workbench already created this turn's preparation task. */
 trainingTask?:boolean;onOpenTraining?:()=>void;
}){
 const [notice,setNotice]=useState(''),[error,setError]=useState(''),[busy,setBusy]=useState(false);
 const [policy,setPolicy]=useState<AgentPolicy|null>(null);
 const alive=useRef(true),lock=useRef(false),autoDone=useRef('');
 useEffect(()=>{alive.current=true;return()=>{alive.current=false;};},[]);

 const loadPolicy=useCallback(async()=>{
  try{
   const value=await api<AgentPolicy>('/api/runtime/agent-policy');
   if(alive.current)setPolicy(value);
  }catch{/* policy API is optional in older frozen builds; stay conservative */}
 },[]);
 useEffect(()=>{
  void loadPolicy();
  const refresh=()=>void loadPolicy();
  window.addEventListener('py-agent-policy-changed',refresh);
  return()=>window.removeEventListener('py-agent-policy-changed',refresh);
 },[loadPolicy]);

 const loadWorkflow=useCallback(async(goalId:string)=>{
  try{
   const value=await api<AgentWorkflow>(`/api/career-goals/${goalId}/workflow`);
   if(alive.current)onWorkflow(value);
  }catch(e){if(alive.current)setError(failure(e));}
 },[onWorkflow]);

 useEffect(()=>{
  if(!goal){onWorkflow(null);return;}
  void loadWorkflow(goal.id);
  if(!['PENDING','RUNNING'].includes(goal.status))return;
  const timer=window.setInterval(()=>void loadWorkflow(goal.id),1500);
  return()=>window.clearInterval(timer);
 },[goal?.id,goal?.status,loadWorkflow,onWorkflow]);

 const remember=async(tool:string,decision:'once'|'always'|'reject')=>{
  try{
   const next=await post<AgentPolicy>('/api/runtime/agent-policy/decision',{tool,decision});
   if(alive.current)setPolicy(next);
   // "Always" is followed by an explicit manual execution of this same proposal.
   // Mark it as handled so the new auto effect cannot immediately run it twice.
   if(decision==='always'&&goal)autoDone.current=`${goal.id}:${goal.version}:${tool}`;
   window.dispatchEvent(new CustomEvent('py-agent-policy-changed'));
  }catch(e){if(alive.current)setError(failure(e));}
 };

 const execute=async(decision:'once'|'always'|'auto'='once')=>{
  if(!goal?.action||lock.current)return;
  const kind=goal.action.type,info=proposals[kind];
  if(!info?.cta)return;
  if(kind==='ask_user'){onFocusInput();setNotice('输入焦点已放回上面的输入框；补充后发送，Agent 会接着协调。');return;}
  lock.current=true;setBusy(true);setError('');setNotice('');
  // A deterministic key for auto runs means the same proposal can never be
  // submitted twice, even if the component re-mounts.
  const key=decision==='auto'?`agent-auto-${goal.id}-${goal.version}-${kind}`:crypto.randomUUID();
  try{
   if(kind==='prepare_job_research'){
    await post(`/api/career-goals/${goal.id}/research`,{
     expected_version:goal.version,kind:'search',source_id:'tme-campus',
     keyword:(goal.conditions.direction||'AI 应用').slice(0,80),
    },key);
    if(!alive.current)return;
    setNotice((decision==='auto'?'已按你的授权自动执行：':'已按 Agent 提议把')+
     '研究请求提交给后台研究 Agent；结果出来后会出现在下方产物里，不会把排队说成已完成。');
   }else if(kind==='prepare_materials'||kind==='prepare_training'){
    const stepId=goal.steps.at(-1)?.id;
    if(!stepId||goal.status!=='WAITING_FOR_TOOL'){
     setNotice('Agent 这一轮还在协调，等它给出可确认的提议后再执行；不会替你抢先提交。');
     return;
    }
    const route=kind==='prepare_materials'?'confirm-materials':'confirm-training';
    const body=kind==='prepare_materials'
     ?{expected_version:goal.version,mode:'analyze',source_ids:[],fact_ids:[],continue_on_completion:false}
     :{expected_version:goal.version,role:'ai-application',mode:'interview',
       max_questions:4,duration_seconds:1200,continue_on_completion:false};
    await post(`/api/career-goals/${goal.id}/steps/${stepId}/${route}`,body,key);
    if(!alive.current)return;
    setNotice((decision==='auto'?'已按你的授权自动执行：':'')+
     (kind==='prepare_materials'?'材料分析任务已经入队；':'训练场次已经创建；')+
     '这条确认绑定了 Agent 本轮的提议，不会被当成另一次自发提交。');
    onOpenPane(kind==='prepare_materials'?'materials':'interview');
   }else if(info.pane){
    onOpenPane(info.pane);
    setNotice('已打开对应产物页；申请动作仍需你在那里核对后确认，Agent 不会擅自投递。');
   }
   await loadWorkflow(goal.id);onChanged();
  }catch(e){if(alive.current)setError(failure(e));}
  finally{lock.current=false;if(alive.current)setBusy(false);}
 };

 const kind=goal?.action?.type??'';
 const effect=policy?.effects[kind]??'one_click';
 const autoKey=goal?`${goal.id}:${goal.version}:${kind}`:'';
 useEffect(()=>{
  if(effect!=='auto'){autoDone.current='';return;}
  if(!goal?.action||!policy||autoDone.current===autoKey)return;
  if(!proposals[kind]?.cta)return;
  autoDone.current=autoKey;
  void execute('auto');
 },[autoKey,effect,policy,kind]);

 const last=goal?.steps.at(-1)??null;
 const source=last?.origin==='real_model'?'模型协调（live）':last?.origin==='scripted_protocol'
  ?'免费脚本协调器 · 确定性协议，不代表真实语义理解':'输入已保存，等待后台协调';
 const info=goal?.action?proposals[goal.action.type]:null;
 const risk=policy?.effects[kind]?riskText[(kind==='read_goal'||kind==='read_application'||kind==='prepare_job_research')?'low':'high']:'';

 return <section className="card d-card d-agent" data-testid="agent-console" aria-label="Agent 运行轨道">
  <div className="d-card-head"><h2>Agent 运行轨道</h2>
   <span className="tag">{goal?(phases[goal.status]??goal.status):'待启动'}</span></div>
  <p className="fine-print d-agent-source">来源是确定性协议，不代表真实语义理解；要授权时停下等你确认。</p>
  {policy&&<p className="fine-print d-agent-policy" data-testid="agent-policy-line">
   自治策略：{policy.modes.find(item=>item.id===policy.mode)?.label??policy.mode} · 只保存在本机，不调用付费模型。</p>}
  {error&&<p role="alert" className="error">{error}</p>}
  {notice&&<p className="notice" role="status">{notice}</p>}

  {!goal&&<div className="d-agent-empty">
   <p>在上面写一句求职意向，Agent 会自己决定下一步：先研究岗位、准备材料，还是安排训练。</p>
   <p className="fine-print">不需要先填完所有表单，也不需要自己挨个点开功能面板。方向不清楚也可以开始。</p>
  </div>}

  {goal&&info&&goal.action&&<div className="d-agent-proposal" data-testid="agent-proposal">
   <div><span className="eyebrow">AGENT 当前的下一步{risk?` · ${risk}`:''}</span><h3>{info.title}</h3>
    <p>{goal.action.text}</p>
    {effect==='deny'&&<p className="fine-print" data-testid="agent-denied">按当前自治策略，这类动作不会执行。可在「设置 → Agent 自治」里换一个模式，或撤销这条授权。</p>}
    {effect==='auto'&&<p className="fine-print">这类动作已获你的长期授权（可在「设置 → Agent 自治」撤销），Agent 会自动执行本轮提议。</p>}
   </div>
   {kind==='prepare_training'&&trainingTask&&<div className="d-agent-actions" data-testid="agent-actions">
    <button type="button" className="secondary d-agent-cta" data-testid="agent-open-training"
     onClick={()=>onOpenTraining?.()}>查看本次准备</button>
    <p className="fine-print">这一轮的准备任务已经由工作台创建；在上面确认计划即可开场，不会再建第二场。</p>
   </div>}
   {kind!=='prepare_training'&&effect!=='deny'&&info.cta&&<div className="d-agent-actions" data-testid="agent-actions">
    {effect==='ask'?<>
     <button type="button" className="primary d-agent-cta" data-testid="agent-execute"
      disabled={busy||goal.status==='CANCELLED'} onClick={()=>void (async()=>{await remember(kind,'once');await execute('once');})()}>
      {busy?'正在提交…':'只执行这一次'}</button>
     <button type="button" className="secondary" data-testid="agent-always"
      disabled={busy} onClick={()=>void (async()=>{await remember(kind,'always');await execute('always');})()}>以后这类都自动执行</button>
     <button type="button" className="text-button" data-testid="agent-reject"
      disabled={busy} onClick={()=>void (async()=>{
       await remember(kind,'reject');
       setNotice('已拒绝这条提议，并记住以后不再自动执行同类动作；本轮没有写入任何记录。');
      })()}>拒绝执行</button>
    </>:<button type="button" className="primary d-agent-cta" data-testid="agent-execute"
      disabled={busy||goal.status==='CANCELLED'} onClick={()=>void execute('once')}>
      {busy?'正在提交…':info.cta}</button>}
   </div>}
    {/* F07Z-78: the reviewer could not tell which of the three choices becomes a lasting
        authorization or where to undo it. The card now says both, next to the buttons. */}
    {effect==='ask'&&<p className="fine-print d-agent-persistence" data-testid="agent-persistence">
     「只执行这一次」不会产生长期授权；后两个选择会记进「设置 → Agent 自治」，在那里可以逐条撤销。</p>}
  </div>}

  {goal?.profile&&(goal.profile.direction||goal.profile.cities.length||goal.profile.recruitment)&&
   <div className="d-agent-extract"><h3>Agent 从你的话里提取（待你确认）</h3>
    <p>方向：{goal.profile.direction?.value??'未知'} · 城市：{goal.profile.cities.map(item=>item.value).join('、')||'未知'} · 招聘类型：{goal.profile.recruitment?.value??'未知'}</p>
    <p className="fine-print">提取候选只是建议，不是已确认条件，也不是能力判定。</p></div>}

  {goal&&<ol className="d-agent-timeline" data-testid="agent-timeline">
   {goal.steps.map((step,index)=><li key={step.id} className={index===goal.steps.length-1?'current':''}>
    <span className="d-agent-dot" aria-hidden="true"/>
    <div className="d-agent-turn">
     <div className="d-agent-turn-head"><strong>{step.origin==='tool_result'?'后台任务完成通知':'你的一句话'}</strong>
      <small className="d-num d-num-sm">第 {step.goal_version} 轮 · {phases[step.status]??step.status}</small></div>
     <p>{step.message}</p>
     {step.tool_log&&step.tool_log.length>0&&<p className="fine-print">本轮工具：{step.tool_log.map(item=>
      (toolText[item.tool]??'校验动作')+(item.status==='error'?'（未通过校验）':'')).join(' → ')}</p>}
     <p className="fine-print">模型调用 {step.model_requests} 次{step.error_code?` · 错误 ${step.error_code}`:''}</p>
    </div>
   </li>)}
  </ol>}
 </section>;
}

/** Session states, in words a user can act on; the raw enum never reaches the page. */
const sessionState:Record<string,string>={ACTIVE:'进行中',COMPLETED:'已完成',FAILED:'未完成',
 CANCELLED:'已取消',SUBMITTED:'已交卷'};

export function AgentArtifacts({workflow,onOpenPane,onNewGoal,compact=false}:{
 workflow:AgentWorkflow|null;onOpenPane:(pane:AgentPane)=>void;onNewGoal:()=>void;
 /** The workbench lists the same rows as a summary, so the long explanations stay on the page. */
 compact?:boolean;
}){
 // The workflow endpoint orders these by created_at DESC, so index 0 is the newest session.
 // (Taking the last element would have labelled the oldest one as "最近一场".)
 const newest=(list:{status:string}[])=>list.length?sessionState[list[0].status]
  ??list[0].status:'';
 const materials=workflow?(workflow.materials.source_count===0?'还没有材料'
  :`${workflow.materials.source_count} 份来源 · ${workflow.materials.confirmed_fact_count} 条已确认依据`
  +(workflow.materials.runs.length?` · ${workflow.materials.runs.length} 次改写`:'')):'读取中';
 const interviews=workflow?(workflow.interviews.length
  ?`${workflow.interviews.length} 场 · 最近一场${newest(workflow.interviews)}`:'还没有场次'):'读取中';
 const written=workflow?(workflow.written_tests.length
  ?`${workflow.written_tests.length} 场 · 最近一场${newest(workflow.written_tests)}`:'还没有场次'):'读取中';
 const results:{label:string;value:string;pane:AgentPane}[]=[
  {label:'选定岗位',value:workflow?(workflow.selected_job?.title??'还没有选岗'):'读取中',pane:'research'},
  {label:'材料来源 / 已确认依据',value:materials,pane:'materials'},
  {label:'面试训练',value:interviews,pane:'interview'},
  {label:'笔试',value:written,pane:'written'},
 ];
 const records:{label:string;value:string;pane:AgentPane}[]=[
  {label:'投递与进度',value:'本机事件流',pane:'apps'},
  {label:'题库',value:'本机夹具与导入',pane:'banks'},
 ];
 // A first-run page should say what is missing and what to do, not render four rows of "0".
 const nothingYet=Boolean(workflow)&&!workflow!.selected_job
  &&!workflow!.materials.source_count&&!workflow!.interviews.length&&!workflow!.written_tests.length;
 return <section className="card d-card">
  {!compact&&<p className="fine-print">Agent 已经跑出来的东西，不是要你依次点开的任务；点一行进对应页面看详情。</p>}
  <div className="d-artifact-zone">
   <div className="d-artifact-zone-label">运行结果</div>
   {nothingYet&&<p className="muted d-artifact-empty">还没有产物。在工作台让 Agent 跑一轮，或先去面试/笔试练一场。</p>}
   <div className="d-quick">{results.map(row=><button key={row.pane} type="button"
    className="d-quick-item d-agent-artifact" onClick={()=>onOpenPane(row.pane)}>
    <span className="d-agent-artifact-label">{row.label}</span>
    <span className="d-agent-artifact-value">{row.value}</span><span aria-hidden="true">→</span></button>)}</div>
  </div>
  <div className="d-artifact-zone">
   <div className="d-artifact-zone-label">记录与配置</div>
   {!compact&&<p className="fine-print d-artifact-zone-note">这两项不是 Agent 产出，只是本机记录与配置。</p>}
   <div className="d-quick">{records.map(row=><button key={row.pane} type="button"
    className="d-quick-item d-agent-artifact" onClick={()=>onOpenPane(row.pane)}>
    <span className="d-agent-artifact-label">{row.label}</span>
    <span className="d-agent-artifact-value">{row.value}</span><span aria-hidden="true">→</span></button>)}</div>
  </div>
  <div className="d-artifact-zone d-artifact-zone-action">
   <button type="button" className="secondary" onClick={onNewGoal}>新建求职目标</button>
  </div>
 </section>;
}
