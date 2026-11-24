import {useState} from 'react';
import {ApiError,api,post} from './api.ts';

type WikiStatus='pending'|'active'|'archived'|'withdrawn';
type WikiAction='publish'|'restore'|'withdraw';
type Entry={
 id:string;topic_key:string;version:number;status:WikiStatus;
 document:{title:string;question:string;probe:string;
  public_reference:{title:string;url:string;quote:string}};
};

const path='/api/runtime/interview-wiki';
const statusText:Record<WikiStatus,string>={
 pending:'待审核',active:'已启用',archived:'历史版本',withdrawn:'已撤回'
};

export function InterviewWikiSection(){
 const [entries,setEntries]=useState<Entry[]|null>(null);
 const [busy,setBusy]=useState(false);
 const [error,setError]=useState('');

 async function load(){
  setEntries((await api<{items:Entry[]}>(path)).items);
 }

 async function open(){
  setBusy(true);setError('');
  try{await load();}catch{setError('Wiki 暂时读取失败。关闭后重新展开可再试。');}
  finally{setBusy(false);}
 }

 async function review(entry:Entry,action:WikiAction){
  const active=entries?.find(item=>item.topic_key===entry.topic_key&&item.status==='active');
  setBusy(true);setError('');
  try{
   await post(`${path}/${entry.id}/review`,{action,expected_active_id:active?.id??null});
   await load();
  }catch(value){
   if(value instanceof ApiError&&value.status===409){
    try{await load();setError('版本已变化，请核对后重新选择。');}
    catch{setError('版本已变化，重新展开 Wiki 可读取最新列表。');}
   }else setError('无法更新此 Wiki 版本，请重新打开后再试。');
  }finally{setBusy(false);}
 }

 return <details className="d-set-group" data-testid="interview-wiki"
  onToggle={event=>{if(event.currentTarget.open)void open();}}>
  <summary>面试 Wiki</summary>
  <p className="hint">已问过且带官方资料的题会自动保存。你发布后，相似岗位的完整模拟可以复用。</p>
  {busy&&entries===null&&<p role="status">正在读取 Wiki…</p>}
  {error&&<p className="error" role="alert">{error}</p>}
  {entries?.length===0&&<p className="hint">还没有可审核的题目。</p>}
  {entries?.map(entry=><article className="d-card" key={entry.id} data-testid={`wiki-entry-${entry.status}`}>
   <div className="d-card-head">
    <h3>{entry.document.title}</h3><span className="tag">v{entry.version} · {statusText[entry.status]}</span>
   </div>
   <p>{entry.document.question}</p>
   {entry.document.probe&&<p className="fine-print">追问 · {entry.document.probe}</p>}
   <blockquote>{entry.document.public_reference.quote}</blockquote>
   <a href={entry.document.public_reference.url} target="_blank" rel="noopener noreferrer">
    {entry.document.public_reference.title}
   </a>
   {entry.status!=='withdrawn'&&<div className="d-set-actions">
    {entry.status==='pending'&&<button className="secondary" type="button" disabled={busy}
     onClick={()=>void review(entry,'publish')}>发布供下次练习</button>}
    {entry.status==='archived'&&<button className="secondary" type="button" disabled={busy}
     onClick={()=>void review(entry,'restore')}>恢复这个版本</button>}
    <button className="text-button" type="button" disabled={busy}
     onClick={()=>void review(entry,'withdraw')}>撤回此版本</button>
   </div>}
  </article>)}
 </details>;
}
