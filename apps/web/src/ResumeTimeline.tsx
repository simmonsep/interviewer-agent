import {useCallback,useEffect,useState} from 'react';
import {api,describeError} from './api.ts';

// F07Z-152 phase 3: an index-only directory across resume versions, position bindings, sessions
// and applications. It carries no observation content; those stay behind the session endpoint.
type Item={type:'resume_version'|'position_binding'|'interview_session'|'application';
 at:string;title:string;status?:string;version?:number;resume_version?:number;active?:boolean;
 draft_id?:string;position_id?:string;session_id?:string;application_id?:string;binding_id?:string;
 item_count?:number};
type Detail={kind?:string;id?:string;draft_id?:string;version?:number;text?:string;origin?:string;
 flagged?:boolean;change_note?:string;
 sources?:{id?:string;name?:string;content_sha256?:string}[];
 segments?:{id:string;text:string}[];
 facts?:{id:string;quote:string}[];
 used_by?:{draft_id:string;version:number;index:number}[];
 resume?:{draft_id:string;version:number}|null;
 linked_observations?:string[];
 session_status?:string;
 observations?:{id:string;label:string;summary:string;correction?:string|null}[];
 active?:{resume_version:number;content_sha256?:string}|null;history?:unknown[];
 status?:string;package_revision?:number;evidence_refs?:string[]};
const label:Record<Item['type'],string>={resume_version:'简历版本',position_binding:'岗位绑定',
 interview_session:'面试场次',application:'申请记录'};
// Same visibility rule as the observation endpoint: live interviews hide their observations
// unless the session is explicitly a coaching one.
const TERMINAL=new Set(['COMPLETED','FAILED','CANCELLED','STALE']);
const liveObservationHidden=(status?:string,mode?:string)=>
 !!status&&!TERMINAL.has(status)&&mode!=='coaching';

export function ResumeTimeline({goalId}:{goalId:string}){
 const [items,setItems]=useState<Item[]|null>(null),[error,setError]=useState('');
 const [detail,setDetail]=useState<Detail|null>(null),[detailError,setDetailError]=useState('');
 const load=useCallback(async()=>{
  try{const value=await api<{items:Item[]}>(`/api/career-goals/${goalId}/timeline`);setItems(value.items);setError('');}
  catch(e){setError('时间线暂时读取失败。');}
 },[goalId]);
 useEffect(()=>{void load();},[load]);

 async function open(item:Item){
  setDetailError('');
  try{
   if(item.type==='resume_version'&&item.draft_id&&(item.item_count??0)>0){
    setDetail(await api<Detail>(`/api/career-goals/${goalId}/evidence/resume-bullets/${item.draft_id}/0`));
   }else if(item.type==='position_binding'&&item.position_id){
    setDetail(await api<Detail>(`/api/career-goals/${goalId}/positions/${item.position_id}/binding`));
   }else if(item.type==='application'&&item.application_id){
    setDetail(await api<Detail>(`/api/career-goals/${goalId}/evidence/applications/${item.application_id}`));
   }else if(item.type==='interview_session'&&item.session_id){
    const session=await api<{status:string;policy?:{mode?:string};
     observations?:{id:string;label:string;summary:string;correction?:string|null}[]}>(`/api/career-goals/${goalId}/interviews/${item.session_id}`);
    setDetail({kind:'interview_session',id:String(item.session_id),session_status:session.status,
     observations:liveObservationHidden(session.status,session.policy?.mode)?[]:session.observations??[]});
   }
  }catch(e){setDetailError(describeError(e));}
 }

 async function fetchBullet(draftId:string,index=0){
  setDetailError('');
  try{setDetail(await api<Detail>(`/api/career-goals/${goalId}/evidence/resume-bullets/${draftId}/${index}`));}
  catch(e){setDetailError(describeError(e));}
 }
 async function fetchFact(factId:string){
  setDetailError('');
  try{setDetail(await api<Detail>(`/api/career-goals/${goalId}/evidence/material-facts/${factId}`));}
  catch(e){setDetailError(describeError(e));}
 }
 async function fetchObservation(observationId:string){
  setDetailError('');
  try{setDetail(await api<Detail>(`/api/career-goals/${goalId}/evidence/observations/${observationId}`));}
  catch(e){setDetailError(describeError(e));}
 }

 return <details className="d-timeline" data-testid="resume-timeline"
  onToggle={event=>{if((event.target as HTMLDetailsElement).open)void load();}}>
  <summary>简历与岗位时间线</summary>
  {!items&&!error&&<p className="fine-print">正在读取…</p>}
  {error&&<p className="error" role="alert">{error}</p>}
  {items&&items.length===0&&<p className="fine-print">还没有可显示的记录。</p>}
  {items&&items.length>0&&<ol className="d-timeline-list">
   {items.map(item=><li key={`${item.type}-${item.at}-${item.session_id??item.draft_id??item.binding_id??item.application_id??item.position_id??''}`}>
    <span className="tag">{label[item.type]}</span>
    <span className="d-timeline-title">{item.title}</span>
    {typeof item.resume_version==='number'&&<span className="fine-print"> · 简历 v{item.resume_version}</span>}
    {item.status&&<span className="fine-print"> · {item.status}</span>}
    {item.active===false&&<span className="fine-print"> · 历史绑定</span>}
    <time className="fine-print" dateTime={item.at}>{new Date(item.at).toLocaleString()}</time>
    {((item.type==='resume_version'&&(item.item_count??0)>0)||item.type==='position_binding'||item.type==='application'
      ||(item.type==='interview_session'&&Boolean(item.session_id)))&&
     <button type="button" className="text-button" data-testid={`timeline-open-${item.type}`} onClick={()=>void open(item)}>查看</button>}
   </li>)}
  </ol>}
  {detailError&&<p className="error" role="alert">{detailError}</p>}
  {detail&&<div className="d-card" data-testid="timeline-detail">
   <div className="d-card-head"><h3>{detail.kind??'详情'}</h3>
    <button type="button" className="text-button" onClick={()=>setDetail(null)}>关闭</button></div>
   {detail.text&&<p className="job-text">{detail.text}</p>}
   {detail.change_note&&<p className="fine-print">改动说明：{detail.change_note}</p>}
   {detail.origin&&<p className="fine-print">产生方式：{detail.origin}{detail.flagged?' · 主张检查已拦截':''}</p>}
   {detail.sources&&detail.sources.length>0&&<p className="fine-print">来源：
    {detail.sources.map(source=>source.name??'未命名材料').join(' · ')}</p>}
   {detail.segments&&detail.segments.length>0&&<blockquote>
    <span className="citation-label">引用原文</span><p>{detail.segments[0].text}</p></blockquote>}
   {detail.active&&<p className="fine-print">当前绑定简历 v{detail.active.resume_version}
    {typeof detail.history?.length==='number'?` · 历史 ${detail.history.length} 条`:''}</p>}
   {detail.status&&<p className="fine-print">状态：{detail.status}
    {typeof detail.package_revision==='number'?` · 包版本 ${detail.package_revision}`:''}</p>}
   {detail.facts&&detail.facts.length>0&&<div className="fine-print" data-testid="timeline-facts">关联事实：
    {detail.facts.map(fact=><button key={fact.id} type="button" className="text-button"
      data-testid={`link-fact-${fact.id}`} onClick={()=>void fetchFact(fact.id)}>{fact.quote}</button>)}</div>}
   {detail.used_by&&detail.used_by.length>0&&<div className="fine-print" data-testid="timeline-used-by">被这些简历条目引用：
    {detail.used_by.map(use=><button key={`${use.draft_id}-${use.index}`} type="button" className="text-button"
      data-testid={`link-bullet-${use.draft_id}-${use.index}`}
      onClick={()=>void fetchBullet(use.draft_id,use.index)}>v{use.version} 第 {use.index+1} 条</button>)}</div>}
   {detail.session_status&&<p className="fine-print">场次状态：{detail.session_status}
    {liveObservationHidden(detail.session_status)?' · 进行中的观察不在此展示':''}</p>}
   {detail.observations&&detail.observations.length>0&&<div className="fine-print" data-testid="timeline-session-observations">
    本场观察：
    {detail.observations.map(observation=><button key={observation.id} type="button" className="text-button"
      data-testid={`link-observation-${observation.id}`} onClick={()=>void fetchObservation(observation.id)}>
     {observation.summary||observation.label}</button>)}</div>}
   {detail.resume&&<p className="fine-print">本场所用简历：v{detail.resume.version}
    <button type="button" className="text-button" data-testid="link-resume"
     onClick={()=>void fetchBullet(detail.resume!.draft_id,0)}>查看所用简历条目</button></p>}
   {detail.evidence_refs&&detail.evidence_refs.length>0&&<p className="fine-print">引用：
    {detail.evidence_refs.join(' · ')}</p>}
   {detail.linked_observations&&detail.linked_observations.length>0&&<div className="fine-print">关联观察：
    {detail.linked_observations.map(id=><button key={id} type="button" className="text-button"
      data-testid={`link-observation-${id}`} onClick={()=>void fetchObservation(id)}>查看观察</button>)}</div>}
  </div>}
  <p className="fine-print">只列事件与版本，不包含面试观察内容；观察仍按场次可见性规则展示。</p>
 </details>;
}
