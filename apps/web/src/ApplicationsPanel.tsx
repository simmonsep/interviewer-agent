import {useCallback,useEffect,useRef,useState} from 'react';
import {api,post,ApiError,describeError} from './api.ts';
import {getAsyncActionCopy} from './asyncActionCopy.ts';
import {OperationErrorDetails,errorDetailOf,type OperationErrorDetail} from './OperationErrorDetails.tsx';
import {SiteSyncPanel} from './SiteSyncPanel.tsx';
import {notifyDataChanged} from './dataEvents.ts';

interface Summary{id:string;status:string;version:number}
interface Event{id:string;kind:string;payload:Record<string,unknown>;package_revision:number;created_at:string}
interface Detail{id:string;version:number;status:string;current_package_revision:number;submitted_package_revision:number|null;
 package:{company:string;title:string;entry_url?:string;material:{kind:string}};
 packages:{revision:number;sha256:string;payload:Record<string,unknown>}[];
 events:Event[];
 training_sessions:{id:string;status:string}[];training_observations:unknown[];written_observations:unknown[];
 review:{warnings?:string[];actions?:{kind:string}[]}}

const statusText:Record<string,string>={DRAFT:'草稿',SUBMITTED:'已投递',INTERVIEW:'面试中',OFFER:'已录用',REJECTED:'未通过',WITHDRAWN:'已撤回',UNKNOWN:'状态未知'};
const statusTag:Record<string,string>={DRAFT:'',SUBMITTED:'done',INTERVIEW:'done',OFFER:'done',REJECTED:'bad',WITHDRAWN:'bad',UNKNOWN:''};
const evidenceText:Record<string,string>={user_confirmation:'我自己确认的',receipt_note:'有回执/邮件为证'};
const materialText:Record<string,string>={none:'未附材料',draft:'附了简历草稿',source:'附了原始材料'};

export function ApplicationsPanel({goalId,version,cancelled,active,onAuthLost,onGoalRefresh,goalTitle,onOpenResearch}:{
 goalId:string;version:number;cancelled:boolean;active:boolean;onAuthLost:()=>void;onGoalRefresh:()=>void;
 /** F07Z-143: the confirmed target name, so this page is not context-free. */
 goalTitle?:string|null;
 /** F07Z-145: the page keeps its own guard, and now offers the way out of it. */
 onOpenResearch?:()=>void;
}){
 const [items,setItems]=useState<Summary[]>([]),[detail,setDetail]=useState<Detail|null>(null);
 const [materialRevision,setMaterialRevision]=useState(0),[hasJob,setHasJob]=useState<boolean|null>(null);
 const [company,setCompany]=useState(''),[title,setTitle]=useState(''),[url,setUrl]=useState('');
 const [note,setNote]=useState(''),[nextStatus,setNextStatus]=useState('SUBMITTED'),[evidence,setEvidence]=useState<'user_confirmation'|'receipt_note'>('user_confirmation');
 const [receipt,setReceipt]=useState(''),[availability,setAvailability]=useState('unknown'),[availabilityNote,setAvailabilityNote]=useState('');
 const [error,setError]=useState(''),[notice,setNotice]=useState(''),[busy,setBusy]=useState(false);
 // F07Z-146: technical code stays behind a collapsed disclosure.
 const [errorDetail,setErrorDetail]=useState<OperationErrorDetail|null>(null);
 // F07Z-144: only the create request may switch this button to "正在记录…".
 const [creating,setCreating]=useState(false);
 // F05C：粘贴邮件解析已下线（多此一举）。进度改为从用户已登录的站点页面同步，
 // 见下方 SiteSyncPanel；事件仍由用户确认后写入，状态机不变。
 const alive=useRef(true),lock=useRef(false);

 const load=useCallback(async()=>{
  try{
   const [list,materials,jobs]=await Promise.all([
    api<{items:Summary[]}>(`/api/career-goals/${goalId}/applications`),
    api<{material_revision:number}>(`/api/career-goals/${goalId}/materials`).catch(()=>null),
    api<{selected?:{snapshot_id:string}|null;items:{snapshot_id:string;included:boolean}[]}>(`/api/career-goals/${goalId}/jobs`).catch(()=>null),
   ]);
   if(!alive.current)return;
   setItems(list.items);
   setMaterialRevision(materials?.material_revision??0);
   setHasJob(Boolean(jobs?.selected));
  }catch(e){if(alive.current){if(e instanceof ApiError&&e.status===401)onAuthLost();else setError(describeError(e));}}
 },[goalId,onAuthLost]);

 useEffect(()=>{alive.current=true;void load();return()=>{alive.current=false;};},[load]);
 // The pane mounts while hidden, so a job selected later in the research pane is only
 // picked up when the user actually opens this one.
 useEffect(()=>{if(active)void load();},[active,load]);

 function failure(e:unknown,fallback?:string){
  setErrorDetail(errorDetailOf(e));
  if(e instanceof ApiError&&e.status===401){onAuthLost();return;}
  if(e instanceof ApiError&&e.code==='application_job_required'){setError('投递要绑定一个岗位：先去「岗位研究」选一个岗位，再回来。');return;}
  if(e instanceof ApiError&&e.code==='material_revision_conflict'){setError('材料刚变过，已重新读取，请再提交一次。');void load();return;}
  if(e instanceof ApiError&&e.status===409){setError('目标、材料或投递已更新，已重新读取。');void load();return;}
  if(e instanceof ApiError&&e.status===400){setError('检查一下填写内容：公司、职位必填，链接必须是 https。');return;}
  setError(fallback??describeError(e));
 }
 async function run(action:()=>Promise<void>,fallback?:string){
  if(lock.current)return;lock.current=true;setBusy(true);setError('');setNotice('');setErrorDetail(null);
  try{await action();}catch(e){failure(e,fallback);}finally{lock.current=false;if(alive.current)setBusy(false);}
 }

 const create=()=>{
  setCreating(true);
  return run(async()=>{
  const value=await post<Detail>(`/api/career-goals/${goalId}/applications`,
   {expected_version:version,expected_material_revision:materialRevision,company:company.trim(),title:title.trim(),application_url:url.trim()},
   crypto.randomUUID());
  if(!alive.current)return;
   setDetail(value);setCompany('');setTitle('');setUrl('');
   // Show the success notice only once the list has been refreshed: otherwise the
   // user (and any UI check) sees "recorded" while the item is still absent.
   await load();
   if(!alive.current)return;
   const copy=getAsyncActionCopy('application-save','success');
   setNotice(`${copy.feedbackTitle}。${copy.feedbackBody}`);
   notifyDataChanged('application');
  },getAsyncActionCopy('application-save','error').feedbackBody).finally(()=>{if(alive.current)setCreating(false);});
 };

 const open=(id:string)=>run(async()=>{
  const value=await api<Detail>(`/api/career-goals/${goalId}/applications/${id}`);
  if(alive.current)setDetail(value);
 });

 const progress=()=>run(async()=>{
  if(!detail)return;
  const value=await post<Detail>(`/api/career-goals/${goalId}/applications/${detail.id}/progress`,
   {expected_application_version:detail.version,status:nextStatus,note:note.trim(),evidence_kind:evidence,
    receipt_reference:evidence==='receipt_note'?receipt.trim():''},crypto.randomUUID());
  if(!alive.current)return;
  setDetail(value);setNote('');setReceipt('');
  await load();
  if(!alive.current)return;
  setNotice('状态已更新，事件记录里保留了这次的说明。');notifyDataChanged('application');
 });

 const saveAvailability=()=>run(async()=>{
  if(!detail)return;
  const value=await post<Detail>(`/api/career-goals/${goalId}/applications/${detail.id}/availability`,
   {expected_application_version:detail.version,availability,note:availabilityNote.trim()},crypto.randomUUID());
  if(!alive.current)return;
  setDetail(value);setAvailabilityNote('');
  setNotice('到岗意向已记录。');notifyDataChanged('application');
 });

 return <div className="d-apps">
  <header className="d-pane-head" data-testid="apps-head"><h1>申请跟踪</h1>
   {goalTitle&&<p className="d-pane-goal" data-testid="apps-goal">{goalTitle}</p>}</header>
  {error&&<div className="d-error-block"><p role="alert" className="error">{error}</p>
   <OperationErrorDetails detail={errorDetail}/></div>}
  {notice&&<p className="notice" role="status">{notice}</p>}
  {cancelled&&<p className="notice">当前目标已结束，投递只读。</p>}

  {hasJob===false&&<div className="notice d-app-guard" role="status" data-testid="apps-job-guard">
   <p>还没有选定岗位：投递必须绑定一个岗位。先去「岗位研究」选一个，再回来记录投递。</p>
   {onOpenResearch&&<button type="button" className="secondary" data-testid="apps-open-research"
    onClick={onOpenResearch}>选择岗位</button>}
  </div>}

  <section className="card d-card" aria-label="新增投递">
   <div className="d-card-head"><h2>记录一次投递</h2></div>
   <p className="muted">这里只记录你自己的投递事实，不会替你提交到任何招聘网站。</p>
   <div className="career-fields">
    <label>公司<input maxLength={100} value={company} onChange={e=>setCompany(e.target.value)} placeholder="例如：某科技公司"/></label>
    <label>职位<input maxLength={100} value={title} onChange={e=>setTitle(e.target.value)} placeholder="例如：后端开发实习生"/></label>
   </div>
   <label>投递链接（可选，https）<input maxLength={2048} value={url} onChange={e=>setUrl(e.target.value)} placeholder="https://…"/></label>
   <div className="d-form-foot"><span className="fine-print">创建时为草稿；投递后请用下方“更新状态”记录进展。</span>
    <button type="button" className="primary" disabled={busy||cancelled||!hasJob||!company.trim()||!title.trim()} onClick={()=>void create()}>{creating
   ?getAsyncActionCopy('application-save','submitting').buttonLabel
   :getAsyncActionCopy('application-save','idle').buttonLabel}</button></div>
  </section>

  <section className="card d-card" aria-label="投递列表">
   <div className="d-card-head"><h2>投递</h2><span className="n d-num d-num-sm">{items.length}</span></div>
   {items.length===0?<p className="fine-print">还没有投递记录。</p>:
    <div className="d-session-list">{items.map((item,index)=><button key={item.id} type="button" className="d-session-item"
     onClick={()=>void open(item.id)}>
     <span><strong>投递 {items.length-index} · {item.id.slice(0,8)}</strong><small>版本 v{item.version}</small></span>
     <span className={`tag ${statusTag[item.status]??''}`}>{statusText[item.status]??item.status}</span>
    </button>)}</div>}
  </section>

  <SiteSyncPanel goalId={goalId} cancelled={cancelled} onChanged={()=>void load()}/>

  {detail&&<section className="card d-card" aria-label="投递详情">
   <div className="d-card-head"><h2>{detail.package.company} · {detail.package.title}</h2>
    <span className={`tag ${statusTag[detail.status]??''}`}>{statusText[detail.status]??detail.status}</span></div>
   <p className="muted">材料包第 <span className="d-num d-num-sm">{detail.current_package_revision}</span> 版 · {materialText[detail.package.material.kind]??detail.package.material.kind}
    {detail.package.entry_url&&<> · <a href={detail.package.entry_url} target="_blank" rel="noreferrer">原链接</a>
     <button type="button" className="text-button" title="复制到系统浏览器打开" onClick={()=>void navigator.clipboard?.writeText(detail.package.entry_url!).then(()=>{
      setNotice('链接已复制，可粘贴到浏览器打开。');}).catch(()=>{})}>复制链接</button></>}</p>
   {detail.review?.warnings&&detail.review.warnings.length>0&&
    <p className="fine-print">提示：{detail.review.warnings.join('；')}</p>}

   <div className="d-app-progress">
    <div className="career-fields">
     <label>新状态<select aria-label="新状态" value={nextStatus} onChange={e=>setNextStatus(e.target.value)}>
      {(['DRAFT','SUBMITTED','INTERVIEW','OFFER','REJECTED','WITHDRAWN','UNKNOWN'] as const).map(key=>
       <option key={key} value={key}>{statusText[key]}</option>)}
     </select></label>
     <label>依据<select aria-label="状态依据" value={evidence} onChange={e=>setEvidence(e.target.value as typeof evidence)}>
      <option value="user_confirmation">我自己确认的</option>
      <option value="receipt_note">有回执/邮件为证</option>
     </select></label>
    </div>
    {evidence==='receipt_note'&&<label>回执编号或邮件主题<input maxLength={300} value={receipt} onChange={e=>setReceipt(e.target.value)}/></label>}
    <label>说明<textarea aria-label="状态说明" maxLength={600} value={note} onChange={e=>setNote(e.target.value)} placeholder="例如：HR 约了周三下午一面。"/></label>
    <div className="d-form-foot"><span className="fine-print">状态由你确认；站点同步读到的进度也要经你确认后才写入。</span>
     <button type="button" className="secondary" disabled={busy||cancelled||!note.trim()||(evidence==='receipt_note'&&!receipt.trim())}
      onClick={()=>void progress()}>更新状态</button></div>
   </div>

   <div className="d-app-progress">
    <div className="career-fields">
     <label>到岗意向<select aria-label="到岗意向" value={availability} onChange={e=>setAvailability(e.target.value)}>
      <option value="unknown">尚未确定</option><option value="open">可以考虑</option><option value="closed">暂不考虑</option>
     </select></label>
    </div>
    <label>说明<input maxLength={600} value={availabilityNote} onChange={e=>setAvailabilityNote(e.target.value)} placeholder="例如：暑期可到岗，每周四天。"/></label>
    <div className="d-form-foot"><span className="fine-print">仅记录你的意向，用于后续复盘。</span>
     <button type="button" className="secondary" disabled={busy||cancelled||!availabilityNote.trim()} onClick={()=>void saveAvailability()}>保存意向</button></div>
   </div>

   <div className="d-app-events">
    <h3>事件</h3>
    {detail.events.length===0?<p className="fine-print">还没有状态变更。</p>:
     <ul>{detail.events.map(event=><li key={event.id}>
      <span className="tag">{event.kind==='progress'?'状态':event.kind==='availability'?'到岗':'记录'}</span>
      <span>{String(event.payload.note??'')}</span>
      <small className="d-num d-num-sm">包 v{event.package_revision}</small>
     </li>)}</ul>}
   </div>
   <p className="fine-print">绑定的训练：面试 {detail.training_sessions.length} 场 · 观察 {detail.training_observations.length} 条 · 笔试结论 {detail.written_observations.length} 条。</p>
  </section>}
 </div>;
}
