import {useCallback,useEffect,useRef,useState} from 'react';
import {api,post,ApiError,describeError} from './api.ts';

// Local site progress sync. The backend only reads the DOM of a page the user
// already has open in a browser window on this machine. Nothing is written to
// the database until the user confirms each row here.
interface Adapter{id:string;label:string;entry_url:string;host:string;row:string;company:string;title:string;
 status:string;link:string;status_map:Record<string,string>;verified:boolean}
interface LocalApp{id:string;company:string;title:string;status:string;version:number}
interface SyncRow{company:string;title:string;raw_status:string;suggested_status:string;status_source:string;link:string}
interface SyncResult{adapter_id:string;host:string;page_url:string;captured_at:string;rows:SyncRow[];warnings:string[]}
interface ConfirmRow extends SyncRow{targetId:string;status:string;selected:boolean}
interface Detail{id:string;version:number;status:string;package:{company:string;title:string}}

const STATUS_TEXT:Record<string,string>={DRAFT:'草稿',SUBMITTED:'已投递',INTERVIEW:'面试中',OFFER:'已录用',
 REJECTED:'未通过',WITHDRAWN:'已撤回',UNKNOWN:'状态未知'};
const STATUSES=Object.keys(STATUS_TEXT);
const TRANSITIONS:Record<string,string[]>={
 DRAFT:['SUBMITTED','WITHDRAWN','UNKNOWN'],
 SUBMITTED:['INTERVIEW','OFFER','REJECTED','WITHDRAWN','UNKNOWN'],
 INTERVIEW:['OFFER','REJECTED','WITHDRAWN','UNKNOWN'],
 UNKNOWN:['SUBMITTED','INTERVIEW','OFFER','REJECTED','WITHDRAWN'],
 OFFER:[],REJECTED:[],WITHDRAWN:[],
};

// The server enforces the state machine. A site may report "interview" while the
// local record is still a draft, so we walk via SUBMITTED instead of failing.
function planSteps(current:string,target:string):string[]{
 if(current===target)return [];
 if((TRANSITIONS[current]??[]).includes(target))return [target];
 if(current==='DRAFT'&&target!=='SUBMITTED'&&(TRANSITIONS.SUBMITTED??[]).includes(target))
  return ['SUBMITTED',target];
 return [];
}
function mapToText(map:Record<string,string>):string{
 return Object.entries(map).map(([key,value])=>`${key}=${value}`).join('\n');
}
function textToMap(text:string):Record<string,string>{
 const out:Record<string,string>={};
 for(const line of text.split(/\r?\n/)){
  const at=line.indexOf('=');
  if(at<1)continue;
  const key=line.slice(0,at).trim(),value=line.slice(at+1).trim().toUpperCase();
  if(key&&STATUSES.includes(value))out[key.slice(0,80)]=value;
 }
 return out;
}
function message(error:unknown):string{
 if(error instanceof ApiError)return error.serverMessage??describeError(error);
 return describeError(error);
}

export function SiteSyncPanel({goalId,cancelled,onChanged}:{goalId:string;cancelled:boolean;onChanged:()=>void}){
 const [adapters,setAdapters]=useState<Adapter[]>([]),[selectedId,setSelectedId]=useState('');
 const [configPath,setConfigPath]=useState(''),[profile,setProfile]=useState('');
 const [token,setToken]=useState(''),[bridgeOpen,setBridgeOpen]=useState(false);
 const [locals,setLocals]=useState<LocalApp[]>([]),[rows,setRows]=useState<ConfirmRow[]>([]);
 const [result,setResult]=useState<SyncResult|null>(null),[samples,setSamples]=useState<{tag:string;class:string;id:string;text:string}[]>([]);
 const [error,setError]=useState(''),[notice,setNotice]=useState(''),[busy,setBusy]=useState(false);
 const [editing,setEditing]=useState(false),[draft,setDraft]=useState<Adapter|null>(null),[mapText,setMapText]=useState('');
 const alive=useRef(true),lock=useRef(false);
 const current=adapters.find(item=>item.id===selectedId)??null;

 useEffect(()=>{alive.current=true;return()=>{alive.current=false;};},[]);

 const loadAdapters=useCallback(async()=>{
  try{
   const value=await api<{items:Adapter[];config_path:string;profile:string;bridge_open:boolean}>('/api/desktop/tracking/adapters');
   if(!alive.current)return;
   setAdapters(value.items);setConfigPath(value.config_path);setProfile(value.profile);
   setBridgeOpen(value.bridge_open);
   setSelectedId(old=>value.items.some(item=>item.id===old)?old:(value.items[0]?.id??''));
  }catch(e){if(alive.current)setError(message(e));}
 },[]);

 const loadLocals=useCallback(async()=>{
  try{
   const list=await api<{items:{id:string;status:string;version:number}[]}>(`/api/career-goals/${goalId}/applications`);
   const details=await Promise.all(list.items.map(item=>
    api<Detail>(`/api/career-goals/${goalId}/applications/${item.id}`).catch(()=>null)));
   if(!alive.current)return;
   setLocals(list.items.map((item,index)=>({
    id:item.id,version:item.version,status:item.status,
    company:details[index]?.package.company??'',title:details[index]?.package.title??'',
   })));
  }catch(e){if(alive.current)setError(message(e));}
 },[goalId]);

 useEffect(()=>{void loadAdapters();void loadLocals();},[loadAdapters,loadLocals]);
 useEffect(()=>{
  const item=adapters.find(entry=>entry.id===selectedId);
  setDraft(item?{...item}:null);setMapText(item?mapToText(item.status_map):'');
  setRows([]);setResult(null);setSamples([]);
 },[selectedId,adapters]);

 const run=async(action:()=>Promise<void>)=>{
  if(lock.current)return;lock.current=true;setBusy(true);setError('');setNotice('');
  try{await action();}catch(e){if(alive.current)setError(message(e));}
  finally{lock.current=false;if(alive.current)setBusy(false);}
 };

 const openSite=()=>run(async()=>{
  if(!current)return;
  const value=await post<{token:string;message:string}>('/api/desktop/tracking/open',{adapter_id:current.id});
  if(!alive.current)return;
  setToken(value.token);setBridgeOpen(true);setNotice(value.message);
 });

 const disconnect=()=>run(async()=>{
  const value=await post<{message:string}>('/api/desktop/tracking/disconnect',{});
  if(!alive.current)return;
  setToken('');setBridgeOpen(false);setRows([]);setResult(null);setNotice(value.message);
 });

 const sync=()=>run(async()=>{
  if(!current)return;
  const value=await post<SyncResult>('/api/desktop/tracking/sync',{adapter_id:current.id,token});
  if(!alive.current)return;
  await loadLocals();
  const fresh=await api<{items:{id:string;status:string;version:number}[]}>(`/api/career-goals/${goalId}/applications`);
  const details=await Promise.all(fresh.items.map(item=>
   api<Detail>(`/api/career-goals/${goalId}/applications/${item.id}`).catch(()=>null)));
  const known=fresh.items.map((item,index)=>({id:item.id,version:item.version,status:item.status,
   company:details[index]?.package.company??'',title:details[index]?.package.title??''}));
  setLocals(known);
  const match=(row:SyncRow)=>{
   const name=row.company.trim();
   return known.find(item=>item.company&&name&&
    (item.company.includes(name)||name.includes(item.company)||item.title&&name.includes(item.title)))?.id??'';
  };
  setResult(value);
  setRows(value.rows.map(row=>({...row,targetId:match(row),status:row.suggested_status,
   selected:row.status_source==='mapped'&&row.suggested_status!=='UNKNOWN'})));
  setNotice(`已读取 ${value.rows.length} 条。请逐条核对站点原文与对应投递，确认后才会写入事件流。`);
 });

 const probe=()=>run(async()=>{
  if(!current)return;
  const value=await post<{samples:{tag:string;class:string;id:string;text:string}[];message:string}>(
   '/api/desktop/tracking/probe',{adapter_id:current.id,token});
  if(!alive.current)return;
  setSamples(value.samples);setNotice(value.message);
 });

 const saveAdapter=()=>run(async()=>{
  if(!draft)return;
  const next:Adapter={...draft,status_map:textToMap(mapText)};
  const list=adapters.map(item=>item.id===next.id?next:item);
  const value=await api<{items:Adapter[];config_path:string}>('/api/desktop/tracking/adapters',
   {method:'PUT',body:JSON.stringify({adapters:list})});
  if(!alive.current)return;
  setAdapters(value.items);setConfigPath(value.config_path);setEditing(false);
  setNotice('站点配置已保存在本机；这类规则只影响本机读取，不会上传到任何服务。');
 });

 const addAdapter=()=>run(async()=>{
  const id=`site-${crypto.randomUUID().slice(0,8)}`;
  const blank:Adapter={id,label:'新站点',entry_url:'https://example.com/',host:'example.com',
   row:'',company:'',title:'',status:'',link:'',status_map:{},verified:false};
  const value=await api<{items:Adapter[]}>('/api/desktop/tracking/adapters',
   {method:'PUT',body:JSON.stringify({adapters:[...adapters,blank]})});
  if(!alive.current)return;
  setAdapters(value.items);setSelectedId(id);setEditing(true);
  setNotice('已新增站点模板。请先填入口网址，打开站点并用「探测页面结构」核对选择器。');
 });

 const removeAdapter=()=>run(async()=>{
  if(!current||adapters.length<2)return;
  const value=await api<{items:Adapter[]}>('/api/desktop/tracking/adapters',
   {method:'PUT',body:JSON.stringify({adapters:adapters.filter(item=>item.id!==current.id)})});
  if(!alive.current)return;
  setAdapters(value.items);setSelectedId(value.items[0]?.id??'');setEditing(false);
  setNotice('站点配置已删除。');
 });

 const confirm=()=>run(async()=>{
  if(!result)return;
  const chosen=rows.filter(row=>row.selected&&row.targetId);
  if(chosen.length===0){setError('没有勾选任何条目，或条目还没选对应的本地投递。');return;}
  let written=0;const skipped:string[]=[];
  for(const row of chosen){
   const local=locals.find(item=>item.id===row.targetId);
   if(!local)continue;
   const steps=planSteps(local.status,row.status);
   if(steps.length===0){skipped.push(`${row.company}：当前「${STATUS_TEXT[local.status]??local.status}」不能直接变为「${STATUS_TEXT[row.status]??row.status}」`);continue;}
   let version=local.version;
   const stamp=new Date(result.captured_at).toLocaleString('zh-CN');
   for(const step of steps){
    const value=await post<Detail>(`/api/career-goals/${goalId}/applications/${local.id}/progress`,{
     expected_application_version:version,status:step,
     note:`站点同步：${current?.label??result.host} 显示「${row.raw_status}」，读取于 ${stamp}；本人确认写入。`.slice(0,600),
     evidence_kind:'site_sync',receipt_reference:`${result.host} | ${row.raw_status}`.slice(0,300),
    },crypto.randomUUID());
    version=value.version;
   }
   written+=1;
  }
  if(!alive.current)return;
  setRows([]);setResult(null);
  onChanged();await loadLocals();
  setNotice(`已写入 ${written} 条状态事件${skipped.length?`；跳过 ${skipped.length} 条（${skipped.join('；')}）`:''}。事件里保留了站点原文与读取时间。`);
 });

 return <section className="card d-card d-sync" aria-label="站点进度同步">
  <div className="d-card-head"><h2>站点进度同步</h2><span className="tag">仅本机</span></div>
  <p className="fine-print">打开一个独立的本机浏览器窗口，你用自己的账号登录招聘网站并停留在投递列表页；点「同步当前页」读取这一页上显示的进度，逐条确认后才写入本地事件流。应用不读取账号密码、不保存 Cookie 或令牌、不缓存网页正文、不向任何外部服务上传数据；不自动翻页、不绕过验证码或风控。是否对某个站点启用由你决定，请自行确认该站点的使用条款。</p>
  {error&&<p role="alert" className="error">{error}</p>}
  {notice&&<p className="notice" role="status">{notice}</p>}

  <div className="career-fields">
   <label>站点<select aria-label="同步站点" value={selectedId} onChange={e=>setSelectedId(e.target.value)}>
    {adapters.map(item=><option key={item.id} value={item.id}>{item.label}{item.verified?'（本机已验证）':'（未验证）'}</option>)}
   </select></label>
   <label>状态<span className="fine-print">{bridgeOpen?'已打开本机站点窗口':'尚未打开站点窗口'}</span></label>
  </div>
  <div className="d-form-foot d-sync-actions">
   <button type="button" className="primary" disabled={busy||!current} onClick={()=>void openSite()}>打开站点</button>
   <button type="button" className="secondary" disabled={busy||!current||!token} onClick={()=>void sync()}>同步当前页</button>
   <button type="button" className="secondary" disabled={busy||!current||!token} onClick={()=>void probe()}>探测页面结构</button>
   <button type="button" className="text-button" disabled={busy||!bridgeOpen} onClick={()=>void disconnect()}>停止读取</button>
  </div>
  <p className="fine-print">配置保存在本机：<span className="d-num d-num-sm">{configPath||'未加载'}</span>；浏览器登录态目录：<span className="d-num d-num-sm">{profile||'未加载'}</span>。想彻底清除登录态，关闭窗口后删除该目录即可。</p>

  <details className="d-sync-config" open={editing} onToggle={e=>setEditing((e.currentTarget as HTMLDetailsElement).open)}>
   <summary>站点适配器（本机编辑）</summary>
   {/* Advanced adapter fields are mounted only while expanded: the申请面板 also has a
       "公司" input, and keeping a hidden duplicate in the DOM makes label-based
       selectors ambiguous for every existing acceptance suite. */}
   {editing&&draft&&<div className="d-sync-fields">
    <label>名称<input maxLength={120} value={draft.label} onChange={e=>setDraft({...draft,label:e.target.value})}/></label>
    <label>入口网址<input maxLength={400} value={draft.entry_url} onChange={e=>setDraft({...draft,entry_url:e.target.value})}/></label>
    <label>行选择器<input aria-label="行选择器" maxLength={300} value={draft.row} placeholder=".application-row" onChange={e=>setDraft({...draft,row:e.target.value})}/></label>
    <label>公司选择器<input aria-label="公司选择器" maxLength={300} value={draft.company} placeholder=".company" onChange={e=>setDraft({...draft,company:e.target.value})}/></label>
    <label>职位选择器<input aria-label="职位选择器" maxLength={300} value={draft.title} placeholder=".title" onChange={e=>setDraft({...draft,title:e.target.value})}/></label>
    <label>状态选择器<input aria-label="状态选择器" maxLength={300} value={draft.status} placeholder=".status" onChange={e=>setDraft({...draft,status:e.target.value})}/></label>
    <label>链接选择器<input aria-label="链接选择器" maxLength={300} value={draft.link} placeholder="a.job-link" onChange={e=>setDraft({...draft,link:e.target.value})}/></label>
    <label>状态映射（每行 站点原文=本地状态）<textarea aria-label="状态映射" maxLength={2000} value={mapText}
     placeholder={'面试=INTERVIEW\n已投递=SUBMITTED'} onChange={e=>setMapText(e.target.value)}/></label>
    <div className="d-form-foot">
     <span className="fine-print">可选状态：{STATUSES.join(' / ')}。真实站点的选择器要对照你已登录的页面填写，仓库不预置未经验证的站点规则。</span>
     <span className="d-sync-actions">
      <button type="button" className="secondary" disabled={busy} onClick={()=>void saveAdapter()}>保存配置</button>
      <button type="button" className="text-button" disabled={busy} onClick={()=>void addAdapter()}>新增站点</button>
      <button type="button" className="text-button" disabled={busy||adapters.length<2} onClick={()=>void removeAdapter()}>删除此站点</button>
     </span>
    </div>
   </div>}
  </details>

  {samples.length>0&&<div className="d-sync-probe">
   <h3>当前页面元素样本</h3>
   <p className="fine-print">用来写选择器，不保存、不写库。</p>
   <ul>{samples.slice(0,12).map((sample,index)=><li key={index}>
    <code>{sample.tag}{sample.class?`.${sample.class.trim().replace(/\s+/g,'.')}`:''}{sample.id?`#${sample.id}`:''}</code>
    <span>{sample.text}</span></li>)}</ul>
  </div>}

  {result&&<div className="d-sync-results">
   <h3>读取结果 · {result.rows.length} 条</h3>
   <p className="fine-print">来源 {result.host} · 读取时间 {new Date(result.captured_at).toLocaleString('zh-CN')} · 页面 {result.page_url}</p>
   {result.warnings.map((warning,index)=><p key={index} className="notice">{warning}</p>)}
   {rows.map((row,index)=><div className="d-sync-row" key={`${row.company}-${index}`}>
    <label className="d-sync-check"><input type="checkbox" checked={row.selected} disabled={cancelled}
     onChange={e=>setRows(old=>old.map((item,i)=>i===index?{...item,selected:e.target.checked}:item))}/>
     <span><strong>{row.company||'（无公司名）'}</strong> · {row.title||'（无职位名）'}</span></label>
    <p className="fine-print">站点原文：{row.raw_status||'（空）'}
     {row.status_source==='unrecognized'&&<strong> · 未匹配映射，请人工判断</strong>}</p>
    <div className="career-fields">
     <label>写入状态<select aria-label={`第${index+1}条写入状态`} value={row.status} disabled={cancelled}
      onChange={e=>setRows(old=>old.map((item,i)=>i===index?{...item,status:e.target.value}:item))}>
      {STATUSES.map(key=><option key={key} value={key}>{STATUS_TEXT[key]}</option>)}</select></label>
     <label>对应本地投递<select aria-label={`第${index+1}条对应投递`} value={row.targetId} disabled={cancelled}
      onChange={e=>setRows(old=>old.map((item,i)=>i===index?{...item,targetId:e.target.value}:item))}>
      <option value="">不写入（跳过）</option>
      {locals.map(item=><option key={item.id} value={item.id}>{item.company||'未命名'} · {item.title||'—'} · {STATUS_TEXT[item.status]??item.status}</option>)}
     </select></label>
    </div>
   </div>)}
   <div className="d-form-foot"><span className="fine-print">只有勾选且选了对应投递的条目才会写入；写入仍受原有状态机约束。</span>
    <button type="button" className="primary" disabled={busy||cancelled||!rows.some(row=>row.selected&&row.targetId)} onClick={()=>void confirm()}>确认写入事件流</button></div>
  </div>}
 </section>;
}
