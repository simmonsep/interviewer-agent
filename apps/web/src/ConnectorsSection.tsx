import {useCallback,useEffect,useState} from 'react';
import {ApiError,api,describeError} from './api.ts';

// Settings -> 连接器. Each read-only connector has an independent opt-in.
type Item={topic_id:string;title:string;url:string};
type SearchProvider={id:string;label:string;endpoint:string;key_source:'vault'|'environment'|'missing'};
type SearchSettings={provider:string;vault_available:boolean;providers:SearchProvider[]};
type SearchUpdate={provider:string;endpoint:string;api_key?:string;clear_key?:boolean};
type Connector={id:string;label:string;description:string;data_scope:string;domains:string[];
 enabled:boolean;items?:Item[];search_settings?:SearchSettings};
type Listing={connectors:Connector[];storage:string};
type CheckResult={url:string;status:'ok'|'not_found'|'unreachable'|'error'|'blocked';
 http_status:number|null;final_url:string|null;title:string;reason:string|null;checked_at:string};

const connectorErrorText:Record<string,string>={
 connector_disabled:'这个连接器还没启用，先打开开关再核验。',
 reference_url_not_allowed:'只允许题库自带的官方网址（https + 白名单域名）。',
 connector_unknown:'未知的连接器。',
 experience_search_endpoint_not_allowed:'该搜索地址尚未在本机设置中授权。',
 experience_search_key_invalid:'搜索 Key 格式无效，请检查空格或换行。',
 experience_search_key_vault_unavailable:'系统凭据库不可写。请用本机进程环境配置搜索 Key。',
};
const statusText:Record<CheckResult['status'],string>={
 ok:'可以打开',not_found:'链接不存在（404/410）',unreachable:'网络不可达，未核验',
 error:'服务端返回错误，未核验',blocked:'地址不在白名单，已拒绝',
};

function explain(error:unknown):string{
 if(error instanceof ApiError&&connectorErrorText[error.code])return connectorErrorText[error.code];
 return describeError(error);
}

const keyLabels={vault:'已保存到系统凭据库',environment:'已由本机环境配置',missing:'未配置 Key'};

function ExperienceSearchSettings({config,disabled,onSave}:{
 config:SearchSettings;disabled:boolean;onSave:(value:SearchUpdate)=>void;
}){
 const [selected,setSelected]=useState(config.provider);
 const [key,setKey]=useState('');
 const choice=config.providers.find(item=>item.id===selected);
 function save(clear=false){
  if(!choice)return;
  onSave({provider:choice.id,endpoint:choice.endpoint,...(clear?{clear_key:true}:{api_key:key})});
  setKey('');
 }
 return <form className="d-connector-items" data-testid="connector-experience-settings"
  onSubmit={event=>{event.preventDefault();save();}}>
  <label className="d-set-row">
   <span>搜索服务</span>
   <select value={choice?.id??''} disabled={disabled} onChange={event=>{setSelected(event.target.value);setKey('');}}>
    {!choice&&<option value="">请选择可用服务</option>}
    {config.providers.map(item=><option key={item.id} value={item.id}>{item.label}</option>)}
   </select>
  </label>
  {choice&&<>
   <p className="fine-print">{choice.endpoint} · {keyLabels[choice.key_source]}</p>
   {config.vault_available?<label className="d-set-row">
    <span>搜索 API Key</span>
    <input type="password" value={key} autoComplete="off" maxLength={400} disabled={disabled}
     placeholder={choice.key_source==='missing'?'输入你的搜索 Key':'留空沿用已配置的 Key'}
     onChange={event=>setKey(event.target.value)}/>
   </label>:<p className="fine-print">请在运行工作台的本机环境中配置搜索 Key。</p>}
  </>}
  <div className="d-set-actions">
   <button type="submit" className="secondary" disabled={disabled||!choice}>保存搜索配置</button>
   {choice?.key_source==='vault'&&<button type="button" className="text-button" disabled={disabled}
    onClick={()=>save(true)}>清除已保存的 Key</button>}
  </div>
 </form>;
}

export function ConnectorsSection(){
 const [listing,setListing]=useState<Listing|null>(null);
 const [results,setResults]=useState<Record<string,CheckResult>>({});
 const [busy,setBusy]=useState<string>('');
 const [error,setError]=useState('');

 const load=useCallback(async()=>{
  try{setListing(await api<Listing>('/api/runtime/connectors'));}catch(e){setError(explain(e));}
 },[]);
 useEffect(()=>{void load();},[load]);

 async function toggle(connector:Connector){
  setBusy(connector.id);setError('');
  try{
   setListing(await api<Listing>(`/api/runtime/connectors/${connector.id}`,
    {method:'PUT',body:JSON.stringify({enabled:!connector.enabled})}));
  }catch(e){setError(explain(e));}
  finally{setBusy('');}
 }

 async function run(item:Item){
  setBusy(item.url);setError('');
  try{
   const value=await api<CheckResult>('/api/runtime/connectors/reference-check',
    {method:'POST',body:JSON.stringify({url:item.url})});
   setResults(previous=>({...previous,[item.url]:value}));
  }catch(e){setError(explain(e));}
  finally{setBusy('');}
 }

 async function saveSearch(value:SearchUpdate){
  setBusy('experience-search');setError('');
  try{
   setListing(await api<Listing>('/api/runtime/connectors/experience-search/settings',
    {method:'PUT',body:JSON.stringify(value)}));
  }catch(e){setError(explain(e));}
  finally{setBusy('');}
 }

 return <section className="d-set-group" data-testid="connectors-section">
  <h2>连接器</h2>
  {listing&&listing.connectors.map(connector=><div key={connector.id} className="d-card"
    data-testid={`connector-${connector.id}`}>
   <div className="d-card-head"><h3>{connector.label}</h3>
    <span className={`tag ${connector.enabled?'done':'bad'}`}>{connector.enabled?'已启用':'未启用'}</span></div>
   <p className="fine-print">{connector.description}</p>
   <p className="fine-print" data-testid="connector-data-scope">{connector.data_scope}</p>
   <div className="d-set-row"><span>允许的域名</span><span className="val d-num d-num-sm">
    {connector.domains.join(' · ')}</span></div>
   <div className="d-set-actions">
    <button type="button" className="secondary" disabled={busy!==''}
     onClick={()=>void toggle(connector)} data-testid={`connector-toggle-${connector.id}`}>
     {connector.enabled?'关闭连接器':'启用连接器'}</button>
   </div>
   {connector.search_settings&&<ExperienceSearchSettings key={connector.search_settings.provider}
    config={connector.search_settings} disabled={busy!==''} onSave={value=>void saveSearch(value)}/>}
   {connector.enabled&&connector.items&&connector.items.length>0&&
    <div className="d-connector-items" data-testid="connector-items">
     {connector.items.map(item=>{
      const result=results[item.url];
      return <div className="d-set-row" key={item.topic_id}>
       <span>{item.title}</span>
       <span className="val">
        {result&&<span className={`tag ${result.status==='ok'?'done':'bad'}`}
          data-testid={`connector-result-${item.topic_id}`}>{statusText[result.status]}
          {result.http_status?` (${result.http_status})`:''}</span>}
        <a href={item.url} target="_blank" rel="noopener noreferrer">官方文档 ↗</a>
        <button type="button" className="text-button" disabled={busy!==''}
         onClick={()=>void run(item)} data-testid={`connector-check-${item.topic_id}`}>核验</button>
       </span></div>;
     })}
    </div>}
  </div>)}
  {error&&<p className="error" role="alert" data-testid="connectors-error">{error}</p>}
 </section>;
}
