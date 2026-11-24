import {useEffect,useState} from 'react';
import {api} from './api.ts';
import {onDataChanged} from './dataEvents.ts';

// B+ : the eight rail entries are result views, not a task menu. Every badge is
// computed from a real read of this machine's API. A missing or failed read is
// reported as missing, never replaced with a placeholder number.

export interface RailBadges{
 materials?:string;interviews?:string;written?:string;research?:string;apps?:string;
}
export interface RailCounts{
 drafts:number;materialPending:number;sources:number;sessions:number;activeSessions:number;
 written:number;researchRuns:number;researchSelected:boolean;applications:number;
}
export interface RailSignals{badges:RailBadges;counts:RailCounts|null;error:string|null;loaded:boolean}

const initial:RailSignals={badges:{},counts:null,error:null,loaded:false};

interface MaterialView{sources:unknown[];runs:{id:string;status:string}[];drafts:{id:string}[];material_revision:number}
interface SessionList{items:{id:string;status:string}[]}
interface WorkflowView{selected_job:{title:string}|null;interviews:unknown[];written_tests:{id:string;status:string}[]}
interface JobsView{selected:unknown|null;runs:{id:string;status:string}[]}
interface AppsView{items:unknown[]}

const terminal=new Set(['COMPLETED','FAILED','CANCELLED','SUCCEEDED']);
const busy=new Set(['PENDING','RUNNING','QUEUED','WAITING_FOR_TOOL']);

export function useRailSignals(goal:{id:string;version:number;status:string}|null):RailSignals{
 const [state,setState]=useState<RailSignals>(initial);
 useEffect(()=>{
  let alive=true;
  if(!goal){setState(initial);return;}
  const load=async()=>{
   try{
    const [materials,sessions,workflow,jobs,apps]=await Promise.all([
     api<MaterialView>(`/api/career-goals/${goal.id}/materials`),
     api<SessionList>(`/api/career-goals/${goal.id}/interviews`),
     api<WorkflowView>(`/api/career-goals/${goal.id}/workflow`),
     api<JobsView>(`/api/career-goals/${goal.id}/jobs`),
     api<AppsView>(`/api/career-goals/${goal.id}/applications`)
    ]);
    if(!alive)return;
    const drafts=materials.drafts?.length??0;
    const materialPending=(materials.runs??[]).filter(run=>busy.has(run.status)).length;
    const items=sessions.items??[];
    const activeSessions=items.filter(item=>!terminal.has(item.status)).length;
    const written=workflow.written_tests?.length??0;
    const researchRuns=jobs.runs?.length??0;
    const researchSelected=Boolean(jobs.selected??workflow.selected_job);
    const applications=apps.items?.length??0;
    const counts:RailCounts={drafts,materialPending,sources:materials.sources?.length??0,
     sessions:items.length,activeSessions,written,researchRuns,researchSelected,applications};
    const badges:RailBadges={};
    if(materialPending>0)badges.materials=`${materialPending} 项在跑`;
    else if(drafts>0)badges.materials=`${drafts} 版初稿`;
    if(activeSessions>0)badges.interviews=`${activeSessions} 场进行中`;
    else if(items.length>0)badges.interviews=`${items.length} 场`;
    if(written>0)badges.written=`${written} 场`;
    if(researchSelected)badges.research='已选岗';
    else if(researchRuns>0)badges.research=`${researchRuns} 次研究`;
    if(applications>0)badges.apps=`${applications} 条`;
    setState({badges,counts,error:null,loaded:true});
   }catch{
    // Keep the last real reading but make the gap visible; never invent zero.
    if(alive)setState(previous=>({...previous,error:'本机部分读数未取到，角标不猜测'}));
   }
  };
  void load();
  const timer=window.setInterval(()=>void load(),6000);
  const onResult=()=>void load();
  window.addEventListener('py-agent-result',onResult);
  // F07Z-148: a finished session must drop the "1 场进行中" badge immediately, not on the next
  // 6s tick. Same re-read as the interval - the badge is never written locally.
  const offData=onDataChanged(()=>void load());
  return()=>{alive=false;window.clearInterval(timer);window.removeEventListener('py-agent-result',onResult);offData();};
 },[goal?.id,goal?.version,goal?.status]);
 return state;
}
