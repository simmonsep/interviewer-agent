import {useEffect,useState} from 'react';
import {api,ApiError} from './api.ts';

// Version B of the desktop shell is a job-seeking application with two fixed Agent
// roles (interview, resume), not a general "new conversation" agent. The interview
// role holds both modes, chosen on its setup card; the cards name the exact HTTP
// route, the live readiness read from this machine, and the boundary.
// Nothing here invents a model call: the free scripted workers are protocol
// fixtures and the UI says so.
// F07Z-72: 面试的两种模式共用一个 Agent 卡（模式在开场卡上选），所以是两个角色而不是三个。
export type AgentKind='interview'|'resume';

interface GoalRef{id:string;version:number;status:string}
interface InterviewItem{status:string}
interface MaterialsView{sources:unknown[];runs:{id:string;status:string;origin:string|null}[];drafts:unknown[]}

export const AGENT_LABEL:Record<AgentKind,string>={
 interview:'面试 Agent',resume:'简历修改 Agent'
};

const kinds:AgentKind[]=['interview','resume'];
// Only what the card actually shows. Route names, frozen request params and the
// "scripted fixture" disclaimers are engineering detail: they are recorded in the
// hand-off report and summarised once in 设置 → 关于本版本, never per card.
const copy:Record<AgentKind,{tag:string;intro:string;launch:string}> = {
 interview:{tag:'实战 / 陪练',intro:'完成真实作答，或在陪练模式中获得即时反馈。',
  launch:'进入面试 Agent'},
 resume:{tag:'按真实材料改写',intro:'基于已保存材料整理简历，不编造未发生的经历。',
  launch:'进入简历修改 Agent'}
};

export function AgentDock({goal,onLaunch}:{goal:GoalRef|null;onLaunch:(kind:AgentKind)=>void}){
 const [interviews,setInterviews]=useState<InterviewItem[]|null>(null);
 const [materials,setMaterials]=useState<MaterialsView|null>(null);
 const [error,setError]=useState('');
 useEffect(()=>{
  let alive=true;
  setError('');
  if(!goal){setInterviews(null);setMaterials(null);return;}
  void (async()=>{
   try{
    const [a,b]=await Promise.all([
     api<{items:InterviewItem[]}>(`/api/career-goals/${goal.id}/interviews`),
     api<MaterialsView>(`/api/career-goals/${goal.id}/materials`)
    ]);
    if(alive){setInterviews(a.items??[]);setMaterials(b);}
   }catch(e){
    if(alive)setError(e instanceof ApiError?`本机服务返回 ${e.code}，状态未读到`:'本机服务未响应，状态未读到（不做假状态）');
   }
  })();
  return()=>{alive=false;};
 },[goal?.id,goal?.version]);

 const activeSessions=interviews?.filter(item=>!['COMPLETED','FAILED','CANCELLED'].includes(item.status)).length??null;
 const stateOf=(kind:AgentKind):string=>{
  if(!goal)return '需要先创建求职目标。';
  if(error)return error;
  if(kind==='resume'){
   if(!materials)return '正在读取本机材料状态…';
   return `本机已有 ${materials.sources.length} 段材料 · ${materials.drafts.length} 版初稿`;
  }
  if(!interviews)return '正在读取本机面试状态…';
  return `本机已有 ${interviews.length} 场会话 · ${activeSessions} 场进行中`;
 };

 return <section className="d-agent-dock" data-testid="agent-dock" aria-label="两个求职 Agent 接口">
  <div className="d-dock-head">
   <div><h2>专属 Agent</h2></div>
   <p className="d-dock-caption">也可以直接进入某个角色，单独处理简历或进行面试训练。</p>
  </div>
  <div className="d-agent-grid">
   {kinds.map(kind=>{
    const item=copy[kind];
    return <article key={kind} className="d-agent-card" data-testid={`agent-card-${kind}`} data-agent={kind}>
     <header><span className="d-agent-badge" aria-hidden="true">AI</span><h3>{AGENT_LABEL[kind]}</h3><span className="tag">{item.tag}</span></header>
     <p className="d-agent-intro">{item.intro}</p>
     <p className="d-agent-state" data-testid={`agent-state-${kind}`}>{stateOf(kind)}</p>
     <div className="d-agent-actions">
      <button type="button" className="primary" data-testid={`agent-launch-${kind}`}
       disabled={!goal} onClick={()=>onLaunch(kind)}>{item.launch}</button>
     </div>
     {/* The route, the frozen mode and the boundary are audit facts, not product
         copy. They live in 设置 → 关于本版本 so the card face stays readable; the
         disclosure is one click away and still verifiable, not deleted. */}
    </article>;
   })}
  </div>
 </section>;
}
