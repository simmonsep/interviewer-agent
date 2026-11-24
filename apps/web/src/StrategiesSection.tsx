import {useCallback,useEffect,useState} from 'react';
import {ApiError,api,describeError} from './api.ts';

// Settings -> 追问策略. A card says *how to probe a project*, never a question with an answer.
// Import is a declarative JSON document; versions are append-only and old sessions keep their own.
type Card={id:string;title:string;dimensions:string[];example_question:string};
type Preview={preview_id:string;document:{id?:string;name:string;role:string;cards:Card[]};
 content_sha256:string;requires_confirmation:boolean;review:string};
type Version={strategy_set_id:string;role:string;version:number;enabled:boolean;content_sha256:string;
 name:string;card_count:number};
type StrategySet={strategy_set_id:string;role:string;active_version:number|null;enabled:boolean;
 versions:Version[]};
type Listing={strategy_sets:StrategySet[]};

const errorText:Record<string,string>={
 strategy_document_invalid:'文件不是有效的策略 JSON。',
 strategy_schema_version_unsupported:'只支持 schema_version = 1。',
 strategy_name_invalid:'name 为空或超过 80 字。',
 strategy_set_id_invalid:'需要英文 id（小写字母、数字、短横线）。',
 strategy_role_invalid:'role 只能是 java-backend 或 ai-application。',
 strategy_cards_invalid:'cards 需要 1–12 张。',
 strategy_id_invalid:'卡片 id 必须是英文 slug。',
 strategy_id_duplicate:'卡片 id 重复。',
 strategy_title_invalid:'卡片标题为空或超过 40 字。',
 strategy_dimensions_invalid:'dimensions 只能取 contribution/verification/failure/tradeoff。',
 strategy_example_invalid:'示例问题为空或超过 220 字。',
 strategy_id_conflicts_builtin:'卡片 id 与预装策略重名，请改名。',
 strategy_document_too_large:'文档过大。',
 strategy_single_set_limit:'同一角色同时只启用一套用户策略，请先停用另一套。',
 resource_unavailable:'这套策略已不可用。',
};
function explain(error:unknown):string{
 if(error instanceof ApiError&&errorText[error.code])return errorText[error.code];
 return describeError(error);
}

export function StrategiesSection(){
 const [listing,setListing]=useState<Listing|null>(null);
 const [preview,setPreview]=useState<Preview|null>(null);
 const [busy,setBusy]=useState(false);
 const [error,setError]=useState(''),[message,setMessage]=useState('');

 async function load(){try{setListing(await api<Listing>('/api/strategies'));}catch(e){setError(explain(e));}}
 useEffect(()=>{void load();},[]);

 async function choose(event:React.ChangeEvent<HTMLInputElement>){
  const file=event.target.files?.[0];event.target.value='';
  if(!file)return;
  setBusy(true);setError('');setMessage('');setPreview(null);
  try{
   const document=JSON.parse(await file.text());
   setPreview(await api<Preview>('/api/strategies/imports',
    {method:'POST',body:JSON.stringify({document}),
     headers:{'Content-Type':'application/json','Idempotency-Key':crypto.randomUUID()}}));
  }catch(e){setError(e instanceof SyntaxError?'文件不是有效 JSON。':explain(e));}
  finally{setBusy(false);}
 }

 async function commit(){
  if(!preview)return;
  setBusy(true);setError('');setMessage('');
  try{
   const value=await api<{strategy_set_id:string;version:number;enabled:boolean;note?:string}>(
    `/api/strategies/imports/${preview.preview_id}/commit`,
    {method:'POST',body:JSON.stringify({enabled:true}),
     headers:{'Content-Type':'application/json','Idempotency-Key':crypto.randomUUID()}});
   setMessage(value.enabled
    ?`已导入「${preview.document.name}」v${value.version}，之后开的面试会带上它。`
    :`已导入「${preview.document.name}」v${value.version}，但暂未启用：同一角色只启用一套用户策略。`);
   setPreview(null);await load();
  }catch(e){setError(explain(e));}
  finally{setBusy(false);}
 }

 async function toggle(item:StrategySet){
  setBusy(true);setError('');setMessage('');
  try{
   setListing(await api<Listing>(`/api/strategies/${encodeURIComponent(item.strategy_set_id)}`,
    {method:'PATCH',body:JSON.stringify({enabled:!item.enabled})}));
   setMessage(item.enabled?'已停用：之后开的面试不再使用它，旧场次不受影响。':'已启用。');
  }catch(e){setError(explain(e));}
  finally{setBusy(false);}
 }

 async function remove(item:StrategySet){
  if(busy||!window.confirm('卸载这套策略？已开的场次仍保留原版本。'))return;
  setBusy(true);setError('');setMessage('');
  try{
   setListing(await api<Listing>(`/api/strategies/${encodeURIComponent(item.strategy_set_id)}`,
    {method:'DELETE'}));
   setMessage('已卸载。');
  }catch(e){setError(explain(e));}
  finally{setBusy(false);}
 }

 return <section className="d-set-group" data-testid="strategies-section">
  <h2>追问策略</h2>
  <p className="hint">策略卡说明"怎么追问项目"，不是题目、没有答案。导入后由服务端在项目题前给出一条建议，
   模型可以采纳或跳过；本场会冻结它用的版本。</p>
  <div className="d-set-actions">
   <label className="secondary" style={{cursor:'pointer'}}>
    选择策略 JSON
    <input type="file" accept="application/json,.json" hidden disabled={busy}
      onChange={event=>void choose(event)} data-testid="strategies-input"/>
   </label>
  </div>

  {preview&&<div className="d-card" data-testid="strategies-preview">
   <div className="d-card-head"><h3>{preview.document.name}</h3><span className="tag">待确认</span></div>
   <div className="d-set-row"><span>角色</span><span className="val">{preview.document.role}</span></div>
   <div className="d-set-row"><span>卡片</span><span className="val">{preview.document.cards.length} 张</span></div>
   {preview.document.cards.map(card=><p className="fine-print" key={card.id}>
    {card.title} · {card.dimensions.join(' / ')}</p>)}
   <div className="d-set-actions">
    <button type="button" disabled={busy} onClick={()=>void commit()} data-testid="strategies-commit">确认导入</button>
    <button type="button" className="text-button" disabled={busy} onClick={()=>setPreview(null)}>取消</button>
   </div>
  </div>}

  {listing&&listing.strategy_sets.length===0&&<p className="fine-print" data-testid="strategies-empty">
   还没有导入策略；项目题沿用产品自带的三张卡。</p>}
  {listing&&listing.strategy_sets.map(item=>{
   const active=item.versions.find(version=>version.version===item.active_version)??item.versions[0];
   return <div className="d-card" key={item.strategy_set_id} data-testid={`strategy-${item.strategy_set_id}`}>
    <div className="d-card-head"><h3>{active?.name??item.strategy_set_id}</h3>
     <span className={`tag ${item.enabled?'done':'bad'}`}>{item.enabled?'启用中':'已停用'}</span></div>
    <div className="d-set-row"><span>角色 · 卡片</span><span className="val">{item.role} · {active?.card_count} 张</span></div>
    <div className="d-set-row"><span>版本</span><span className="val">
     {item.versions.map(version=><span key={version.version} className={`tag ${version.version===item.active_version?'done':''}`}>
      v{version.version}{version.version===item.active_version?' 当前':''}</span>)}</span></div>
    <div className="d-set-actions">
     <button type="button" className="secondary" disabled={busy} onClick={()=>void toggle(item)}
      data-testid={`strategy-toggle-${item.strategy_set_id}`}>{item.enabled?'停用':'启用'}</button>
     <button type="button" className="text-button" disabled={busy} onClick={()=>void remove(item)}
      data-testid={`strategy-remove-${item.strategy_set_id}`}>卸载</button>
    </div>
   </div>;
  })}
  {message&&<p className="notice" role="status" data-testid="strategies-message">{message}</p>}
  {error&&<p className="error" role="alert" data-testid="strategies-error">{error}</p>}
 </section>;
}
