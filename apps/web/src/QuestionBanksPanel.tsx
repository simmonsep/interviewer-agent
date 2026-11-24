import {useCallback,useEffect,useRef,useState} from 'react';
import {api,post,ApiError,describeError} from './api.ts';
import {parseQuestionBankTemplate,questionBankTemplate,type BankRole,
 type TemplateDocument,type TemplateProblem} from './qbankTemplate.ts';

interface Bank{bank_id:string;version:number;origin:string;enabled:boolean;revision:number;content_sha256:string;name:string;item_count:number}
interface LearningResource{id:string;topic:string;name:string;blurb:string;url:string;source:'default'|'user'}
interface ResourceList{items:LearningResource[];topics:string[];checked_on:string;note:string}
interface Preview{preview_id:string;document:{name:string;items:{item_id:string}[]};content_sha256:string;requires_confirmation:boolean;
 items_without_reference:string[];choice_items_without_key:string[];programming_items_require_validation:string[];review:string;scope:string}
const sample=`{
  "schema_version": 1,
  "name": "我的练习题",
  "items": [
    {
      "item_id": "slow-query",
      "kind": "knowledge",
      "roles": ["java-backend"],
      "title": "慢查询定位",
      "question": "线上订单列表接口 P95 突然升高，你会按什么顺序定位？",
      "reference": "先看监控与慢日志确认是数据库还是应用层；再看执行计划与索引命中；最后考虑缓存与分页。"
    }
  ]
}`;

interface BankItem{item_id:string;kind:string;roles:string[];title:string;question:string}
interface BankDocument{document:{items:BankItem[]}}

export function QuestionBanksPanel({onAuthLost,goalId,goalVersion,onStarted}:{
 onAuthLost:()=>void;
 /** F07Z-89: the goal a picked question set would belong to; omitted in the browsers where the
  * page has no goal yet, and the picker is hidden. */
 goalId?:string;goalVersion?:number;
 onStarted?:(sessionId:string)=>void;
}){
 const [banks,setBanks]=useState<Bank[]>([]),[error,setError]=useState(''),[notice,setNotice]=useState(''),[busy,setBusy]=useState(false);
 const [raw,setRaw]=useState(''),[preview,setPreview]=useState<Preview|null>(null),[importOpen,setImportOpen]=useState(false);
 // 新增业务 1 · A1: the Markdown path only converts; the server preview and commit stay the same.
 const [mdRaw,setMdRaw]=useState(''),[mdRole,setMdRole]=useState<BankRole>('ai-application');
 const [mdProblems,setMdProblems]=useState<TemplateProblem[]>([]),[mdCount,setMdCount]=useState(0);
 // 新增业务 1 · A2: same bank + new version reuses expected_version; '' means a new bank.
 const [targetBank,setTargetBank]=useState('');
 const [mdWithoutReference,setMdWithoutReference]=useState(0);
 const [resources,setResources]=useState<LearningResource[]>([]),[resourceNote,setResourceNote]=useState(''),[checkedOn,setCheckedOn]=useState('');
 const [newName,setNewName]=useState(''),[newUrl,setNewUrl]=useState(''),[resourceBusy,setResourceBusy]=useState(false);
 // F07Z-89: picking the questions for a session, per bank.
 const [picking,setPicking]=useState<string|null>(null),[pickItems,setPickItems]=useState<BankItem[]>([]);
 const [picked,setPicked]=useState<string[]>([]);
 const [pickRole,setPickRole]=useState<'java-backend'|'ai-application'>('java-backend');
 const [pickBusy,setPickBusy]=useState(false);
 // F07Z-91: build the set from question kinds instead of ticking every item.
 const [kindFilter,setKindFilter]=useState<string[]>([]);
 const [pickCount,setPickCount]=useState(3);
 const alive=useRef(true),lock=useRef(false),previewKey=useRef<string|null>(null);

 const load=useCallback(async()=>{
  try{
   const [bankView,learning]=await Promise.all([
    api<{banks:Bank[]}>('/api/question-banks'),
    api<ResourceList>('/api/desktop/learning-resources'),
   ]);
   if(!alive.current)return;
   setBanks(bankView.banks);
   setResources(learning.items);setResourceNote(learning.note);setCheckedOn(learning.checked_on);
  }
  catch(e){if(alive.current){if(e instanceof ApiError&&e.status===401)onAuthLost();else setError(describeError(e));}}
 },[onAuthLost]);

 // F07Z-86: links open in the user's own browser. The host checks http/https; a browser tab is
 // where an arbitrary address belongs, because its chrome shows the real address.
 async function openResource(url:string){
  const core=(window as unknown as {__TAURI__?:{core?:{invoke<T>(c:string,a?:Record<string,unknown>):Promise<T>}}}).__TAURI__?.core;
  if(core){
   try{await core.invoke('open_external',{url});return;}
   catch(e){setError(e instanceof ApiError?describeError(e):String(e));return;}
  }
  window.open(url,'_blank','noopener,noreferrer');
 }
 async function addResource(){
  if(resourceBusy)return;
  setResourceBusy(true);setError('');setNotice('');
  try{
   const value=await api<{items:LearningResource[]}>('/api/desktop/learning-resources',{method:'POST',
    body:JSON.stringify({name:newName.trim(),url:newUrl.trim()})});
   if(!alive.current)return;
   setResources(value.items);setNewName('');setNewUrl('');
   setNotice(`已添加「${newName.trim()}」，只在这台机器上可见。`);
  }catch(e){
   if(e instanceof ApiError&&e.status===401){onAuthLost();return;}
   if(e instanceof ApiError&&e.code==='learning_resource_exists')setError('这个地址已经在列表里了。');
   else if(e instanceof ApiError&&e.code==='learning_url_scheme_not_allowed')setError('只支持 http:// 或 https:// 开头的地址。');
   else if(e instanceof ApiError&&e.code==='learning_url_credentials_not_allowed')setError('地址里不能带账号密码。');
   else if(e instanceof ApiError&&e.code==='learning_url_host_missing')setError('这个地址没有主机名，请检查。');
   else setError(describeError(e));
  }finally{if(alive.current)setResourceBusy(false);}
 }
 async function removeResource(id:string){
  if(resourceBusy)return;
  setResourceBusy(true);setError('');
  try{
   const value=await api<{items:LearningResource[]}>(`/api/desktop/learning-resources/${id}`,{method:'DELETE'});
   if(alive.current)setResources(value.items);
  }catch(e){
   if(e instanceof ApiError&&e.status===401){onAuthLost();return;}
   setError(describeError(e));
  }finally{if(alive.current)setResourceBusy(false);}
 }

 useEffect(()=>{alive.current=true;void load();return()=>{alive.current=false;};},[load]);

 function failure(e:unknown){
  if(e instanceof ApiError&&e.status===401){onAuthLost();return;}
  if(e instanceof ApiError&&e.code==='builtin_question_bank_read_only'){setError('内置题库是只读的，不能在这里启停。');return;}
  if(e instanceof ApiError&&e.code==='question_bank_revision_conflict'){setError('题库刚被改动过，已重新读取最新状态。');void load();return;}
  if(e instanceof ApiError&&e.code==='question_bank_preview_already_committed'){setError('这次预览已经导入过，请重新预览。');return;}
  if(e instanceof ApiError&&e.status===400){setError('题库文档格式不对：请检查 schema_version、items 与必填字段。');return;}
  setError(describeError(e));
 }
 async function run(action:()=>Promise<void>){
  if(lock.current)return;lock.current=true;setBusy(true);setError('');setNotice('');
  try{await action();}catch(e){failure(e);}finally{lock.current=false;if(alive.current)setBusy(false);}
 }

 const toggle=(bank:Bank)=>run(async()=>{
  const value=await api<{banks:Bank[]}>(`/api/question-banks/${bank.bank_id}`,{method:'PATCH',
   body:JSON.stringify({expected_revision:bank.revision,enabled:!bank.enabled})});
  if(alive.current){setBanks(value.banks);setNotice(`${bank.name} 已${bank.enabled?'停用':'启用'}；面试训练页点「刷新题库」即可看到。`);}
  window.dispatchEvent(new CustomEvent('py-banks-changed'));
 });

 const startPreview=(documentOverride?:unknown)=>run(async()=>{
  let document:unknown=documentOverride;
  if(document===undefined){
   try{document=JSON.parse(raw);}
   catch{setError('不是合法的 JSON，请检查括号与逗号。');return;}
  }
  previewKey.current=crypto.randomUUID();
  const value=await post<Preview>('/api/question-banks/imports',{document},previewKey.current);
  if(alive.current)setPreview(value);
 });
 // Convert first, show the converter's own problems, and only then ask the server to preview.
 // Not wrapped in run(): startPreview already goes through it, and nesting the guard would make
 // the second call a silent no-op.
 const previewMarkdown=()=>{
  const parsed=parseQuestionBankTemplate(mdRaw,mdRole);
  setMdProblems(parsed.problems);setMdCount(parsed.document.items.length);
  setMdWithoutReference(parsed.withoutReference);
  if(parsed.problems.length)return;
  void startPreview(parsed.document satisfies TemplateDocument);
 };
 const downloadTemplate=()=>{
  const blob=new Blob([questionBankTemplate()],{type:'text/markdown;charset=utf-8'});
  const url=URL.createObjectURL(blob);const link=document.createElement('a');
  link.href=url;link.download='题库模板.md';link.click();URL.revokeObjectURL(url);
 };

 const commit=()=>run(async()=>{
  if(!preview)return;
  const [bankId,version]=targetBank.split('|');
  await post(`/api/question-banks/imports/${preview.preview_id}/commit`,
   {bank_id:bankId||null,expected_version:Number(version||0)},crypto.randomUUID());
  if(!alive.current)return;
  setNotice(bankId
   ?`已把「${preview.document.name}」更新为新版本，共 ${preview.document.items.length} 题；旧场次仍读原版本。`
   :`已导入「${preview.document.name}」，共 ${preview.document.items.length} 题。`);
  setPreview(null);setRaw('');setImportOpen(false);setTargetBank('');
  await load();
  window.dispatchEvent(new CustomEvent('py-banks-changed'));
 });

 // --- question picker -------------------------------------------------------
 const needsGoal=picking!==null&&(!goalId||!goalVersion);
 const roleChoices=Array.from(new Set(pickItems.flatMap(item=>item.roles)))
  .filter(role=>role==='java-backend'||role==='ai-application') as ('java-backend'|'ai-application')[];
 const visibleItems=pickItems.filter(item=>item.roles.includes(pickRole)&&item.kind!=='java_programming');
 const kindChoices=Array.from(new Set(visibleItems.map(item=>item.kind)));
 const kindLabels:Record<string,string>={single_choice:'单选',multiple_choice:'多选',
  short_answer:'简答',knowledge:'知识问答'};
 const poolItems=kindFilter.length===0?visibleItems
  :visibleItems.filter(item=>kindFilter.includes(item.kind));

 async function togglePicker(bank:Bank){
  if(picking===bank.bank_id){setPicking(null);setPickItems([]);setPicked([]);return;}
  setPicking(bank.bank_id);setPicked([]);setPickItems([]);setKindFilter([]);setError('');
  try{
   const value=await api<BankDocument>(`/api/question-banks/${bank.bank_id}/versions/${bank.version}`);
   if(!alive.current)return;
   const items=value.document.items.filter(item=>item.kind!=='java_programming');
   setPickItems(items);
   const roles=items.flatMap(item=>item.roles);
   setPickRole(roles.includes('java-backend')?'java-backend':'ai-application');
   setPickCount(Math.min(3,items.filter(item=>item.roles.includes(
    roles.includes('java-backend')?'java-backend':'ai-application')).length||1));
  }catch(e){if(alive.current)setError(describeError(e));}
 }

 function togglePick(itemId:string){
  setPicked(current=>current.includes(itemId)
   ?current.filter(value=>value!==itemId)
   :current.length>=20?current:[...current,itemId]);
 }

 function toggleKind(kind:string){
  setKindFilter(current=>current.includes(kind)
   ?current.filter(value=>value!==kind)
   :[...current,kind]);
 }

 async function startAuto(bank:Bank){
  if(!goalId||!goalVersion)return;
  setPickBusy(true);setError('');setNotice('');
  try{
   const value=await post<{id:string}>(`/api/career-goals/${goalId}/written`,
    {expected_version:goalVersion,role:pickRole,
     question_banks:[{bank_id:bank.bank_id,version:bank.version}],
     ...(kindFilter.length?{item_kinds:kindFilter}:{}),shuffle:true,max_questions:pickCount},
    crypto.randomUUID());
   if(!alive.current)return;
   setNotice(`已随机抽取 ${pickCount} 题开始一场笔试。`);
   setPicking(null);setPickItems([]);setPicked([]);setKindFilter([]);
   onStarted?.(value.id);
  }catch(e){
   if(e instanceof ApiError&&e.status===401){onAuthLost();return;}
   if(e instanceof ApiError&&e.code==='written_in_progress')setError('已经有一场笔试没结束，先完成或取消它。');
   else if(e instanceof ApiError&&e.code==='written_session_limit')setError('这个目标已经有 20 场笔试，不能再开新的。');
   else if(e instanceof ApiError&&e.code==='written_selection_too_few_items')
    setError(`所选题型只有 ${poolItems.length} 题，不够抽 ${pickCount} 题。`);
   else if(e instanceof ApiError&&e.code==='question_bank_kind_not_supported')
    setError('这个题库里没有所选题型的题。');
   else setError(describeError(e));
  }finally{if(alive.current)setPickBusy(false);}
 }

 async function startPicked(bank:Bank){
  if(!goalId||!goalVersion||picked.length===0)return;
  setPickBusy(true);setError('');setNotice('');
  try{
   const value=await post<{id:string}>(`/api/career-goals/${goalId}/written`,
    {expected_version:goalVersion,role:pickRole,
     question_banks:[{bank_id:bank.bank_id,version:bank.version}],
     item_ids:picked,max_questions:picked.length},crypto.randomUUID());
   if(!alive.current)return;
   setNotice(`已按选中的 ${picked.length} 题开始一场笔试。`);
   setPicking(null);setPickItems([]);setPicked([]);
   onStarted?.(value.id);
  }catch(e){
   if(e instanceof ApiError&&e.status===401){onAuthLost();return;}
   if(e instanceof ApiError&&e.code==='written_in_progress')setError('已经有一场笔试没结束，先完成或取消它。');
   else if(e instanceof ApiError&&e.code==='written_session_limit')setError('这个目标已经有 20 场笔试，不能再开新的。');
   else if(e instanceof ApiError&&e.code==='written_item_not_found')setError('题库刚更新过，选中的题已经不在了，请重新打开选题。');
   else setError(describeError(e));
  }finally{if(alive.current)setPickBusy(false);}
 }

 const exportBank=(bank:Bank)=>run(async()=>{
  const value=await api<{document:unknown}>(`/api/question-banks/${bank.bank_id}/versions/${bank.version}`);
  const blob=new Blob([JSON.stringify(value.document,null,2)],{type:'application/json'});
  const url=URL.createObjectURL(blob);
  const link=document.createElement('a');
  link.href=url;link.download=`bank-${bank.name}-v${bank.version}.json`;link.click();
  URL.revokeObjectURL(url);
 });

 return <div className="d-banks">
  {error&&<p role="alert" className="error">{error}</p>}
  {notice&&<p className="notice" role="status">{notice}</p>}

  <section className="card d-card" aria-label="题库列表">
   <div className="d-card-head"><h2>题库</h2><span className="n d-num d-num-sm">{banks.length}</span></div>
   <p className="muted">面试训练从这里取题。内置题库只读；自己导入的题库可以随时启停，停用后不会出现在面试的题库下拉里。</p>
   <div className="d-bank-list">{banks.map(bank=>{
    const builtin=bank.origin==='builtin'||bank.origin==='default';
    return <div className="d-bank" key={bank.bank_id} data-testid={`bank-${bank.name}`}>
     <div className="d-bank-main">
      <strong>{bank.name}</strong>
      <div className="d-bank-meta">
       <span className="tag">{builtin?'内置':'自建'}</span>
       <span className="d-num d-num-sm">v{bank.version} · {bank.item_count} 题</span>
       {!builtin&&<span className={`tag ${bank.enabled?'done':'bad'}`}>{bank.enabled?'已启用':'已停用'}</span>}
      </div>
     </div>
     <div className="d-bank-actions">
      {goalId&&<button type="button" className="text-button" disabled={busy}
        aria-expanded={picking===bank.bank_id} onClick={()=>void togglePicker(bank)}>选题</button>}
      <button type="button" className="text-button" disabled={busy} onClick={()=>void exportBank(bank)}>导出</button>
      {builtin?<span className="fine-print">只读</span>:
       <button type="button" className={`d-switch ${bank.enabled?'is-on':''}`} role="switch" aria-checked={bank.enabled}
        aria-label={`${bank.name} 启用`} disabled={busy} onClick={()=>void toggle(bank)}><span/></button>}
     </div>
    </div>;
   })}</div>
   {picking&&(()=>{
     const bank=banks.find(item=>item.bank_id===picking);
     if(!bank)return null;
     return <div className="d-pick" data-testid="bank-picker">
      <div className="d-pick-head">
       <strong>{bank.name}</strong>
       <span className="fine-print">选中的题会按你勾选的顺序排；最多 20 题。</span>
      </div>
      {roleChoices.length>1&&<label className="d-pick-role">角色
       <select aria-label="选题角色" value={pickRole} onChange={event=>setPickRole(event.target.value as 'java-backend'|'ai-application')}>
        {roleChoices.map(role=><option key={role} value={role}>{role==='java-backend'?'Java 后端':'AI 应用'}</option>)}
       </select></label>}
      {needsGoal&&<p className="muted">先在工作台保存一个求职目标，笔试记录要挂在目标下面。</p>}
      {!needsGoal&&visibleItems.length===0&&<p className="muted">这个题库里没有该角色的题。</p>}
      {!needsGoal&&visibleItems.length>0&&<div className="d-pick-auto" data-testid="bank-auto">
       <div className="d-pick-kinds" role="group" aria-label="题型筛选">
        <span className="fine-print">题型</span>
        {kindChoices.map(kind=>
         <label key={kind} className={`d-kind ${kindFilter.includes(kind)?'is-on':''}`}>
          <input type="checkbox" checked={kindFilter.includes(kind)} disabled={pickBusy}
           onChange={()=>toggleKind(kind)}/>{kindLabels[kind]??kind}
         </label>)}
       </div>
       <div className="d-pick-auto-foot">
        <label className="d-pick-count">抽 <input type="number" min={1} max={20} value={pickCount}
         aria-label="抽取题数" disabled={pickBusy}
         onChange={event=>setPickCount(Math.min(20,Math.max(1,Number(event.target.value)||1)))}/> 题</label>
        <button type="button" className="secondary" data-testid="auto-pick"
         disabled={pickBusy||poolItems.length===0} onClick={()=>void startAuto(bank)}>随机抽题并开始</button>
       </div>
       <p className="fine-print">符合条件的题：<span className="d-num d-num-sm">{poolItems.length}</span>
        {poolItems.length<pickCount?'（不够抽这么多，可减少题数）':'（抽查不重复，抽完再开新场）'}</p>
      </div>}
      {!needsGoal&&<div className="d-pick-list">
       {visibleItems.map(item=>
        <label key={item.item_id} className="d-pick-item">
         <input type="checkbox" checked={picked.includes(item.item_id)} disabled={pickBusy}
          onChange={()=>togglePick(item.item_id)}/>
         <span><strong>{item.title}</strong><small className="fine-print">{item.kind} · {item.item_id}</small></span>
        </label>)}
      </div>}
      {!needsGoal&&<div className="d-form-foot">
       <span className="fine-print">已选 <span className="d-num d-num-sm">{picked.length}</span> 题</span>
       <button type="button" className="primary" disabled={pickBusy||picked.length===0}
        onClick={()=>void startPicked(bank)}>用选中的题开始笔试</button>
      </div>}
     </div>;
   })()}
  </section>

  <section className="card d-card" aria-label="学习资源">
   <div className="d-card-head"><h2>学习资源</h2><span className="n d-num d-num-sm">{resources.length}</span></div>
   <p className="muted">{resourceNote||'题目在这里练，知识在这里补。'}</p>
   <div className="d-resource-zones" data-testid="learning-resources">
    {[...new Set(resources.map(item=>item.topic))].map(topic=>{
     const items=resources.filter(item=>item.topic===topic);
     return <div className="d-artifact-zone" key={topic}>
      <div className="d-artifact-zone-label">{topic}</div>
      <div className="d-quick">{items.map(item=>
       <div className="d-quick-item d-resource" key={item.id} data-testid={`resource-${item.id}`}>
        <button type="button" className="d-resource-open" title={item.url} onClick={()=>void openResource(item.url)}>
         <span className="d-agent-artifact-label">{item.name}</span>
         <small className="fine-print">{item.blurb}</small>
        </button>
        {item.source==='user'
         ?<button type="button" className="text-button" disabled={busy||resourceBusy}
            aria-label={`删除 ${item.name}`} onClick={()=>void removeResource(item.id)}>删除</button>
         :<span className="fine-print" title={`${checkedOn} 核对过可访问`}>已核对</span>}
       </div>)}
      </div>
     </div>;
    })}
   </div>
   <form className="d-resource-form" onSubmit={event=>{event.preventDefault();void addResource();}}>
    <label>名称<input aria-label="学习资源名称" maxLength={60} value={newName}
      placeholder="例如：某门课 / 某个教程" onChange={e=>setNewName(e.target.value)}/></label>
    <label>地址<input aria-label="学习资源地址" maxLength={300} value={newUrl}
      placeholder="https://" onChange={e=>setNewUrl(e.target.value)}/></label>
    <button type="submit" className="secondary" disabled={resourceBusy||!newName.trim()||!newUrl.trim()}>添加</button>
   </form>
   <p className="fine-print">默认条目逐个访问过；你自己添加的链接没有核对，点开前请确认地址。应用只把它交给浏览器打开，不读取页面内容。</p>
  </section>

  <section className="card d-card" aria-label="导入题库">
   <div className="d-card-head"><h2>导入题库</h2>
    <button type="button" className="text-button" onClick={()=>{setImportOpen(!importOpen);setPreview(null);}}>{importOpen?'收起':'展开'}</button></div>
   {!importOpen&&<p className="muted">粘贴题库 JSON，先预览校验结果，确认后才写入。最多 20 个题库、每个 30 个版本。</p>}
   {importOpen&&<>
    <div className="d-import-tools">
     <button type="button" className="text-button" data-testid="md-download-template"
       onClick={downloadTemplate}>下载 Markdown 模板</button>
     <span className="fine-print">一个「## 标题」一题；题干必填，参考可空。</span>
    </div>
    <label htmlFor="md-role">适用训练角色</label>
    <select id="md-role" value={mdRole} disabled={busy}
      onChange={e=>{setMdRole(e.target.value as BankRole);setMdProblems([]);}}>
     <option value="ai-application">AI 应用 / Agent（ai-application）</option>
     <option value="java-backend">Java 后端（java-backend）</option>
    </select>
    <label>Markdown 文档<textarea aria-label="Markdown 题库" className="d-json" data-testid="md-input"
      value={mdRaw} spellCheck={false} placeholder={questionBankTemplate()}
      onChange={e=>{setMdRaw(e.target.value);setMdProblems([]);}}/></label>
    <div className="d-form-foot">
     <span className="muted">已输入 <span className="d-num d-num-sm">{mdRaw.length}</span> 字符</span>
     <button type="button" className="secondary" data-testid="md-preview"
       disabled={busy||!mdRaw.trim()} onClick={()=>void previewMarkdown()}>转换并预览</button>
    </div>
    {mdProblems.length>0&&<div className="d-preview" data-testid="md-problems" role="alert">
     <p><strong>还有 {mdProblems.length} 处需要改</strong>（行号指你粘贴的文档）</p>
     <ul>{mdProblems.map((item,index)=><li key={index}>第 {item.line} 行：{item.message}</li>)}</ul>
    </div>}
    {mdProblems.length===0&&mdCount>0&&<p className="fine-print" data-testid="md-converted">
     已转换 {mdCount} 题{mdWithoutReference>0?`，其中 ${mdWithoutReference} 题没有参考（仍可出题，但不会判对错）`:''}。
    </p>}
    <hr/>
    <p className="muted">一次最多 20 题，也可以直接粘贴题库 JSON。可以先点「填入示例」看格式。</p>
    <div className="d-import-tools">
     <button type="button" className="text-button" onClick={()=>{setRaw(sample);setPreview(null);}}>填入示例</button>
     <span className="fine-print">预览不会写入，也不会占用题库额度。</span>
    </div>
    <label>题库文档<textarea aria-label="题库 JSON" className="d-json" value={raw} spellCheck={false}
      placeholder={sample} onChange={e=>{setRaw(e.target.value);setPreview(null);}}/></label>
    <div className="d-form-foot"><span className="muted">已输入 <span className="d-num d-num-sm">{raw.length}</span> 字符</span>
     <button type="button" className="secondary" disabled={busy||!raw.trim()} onClick={()=>void startPreview()}>预览校验</button></div>
    {preview&&<div className="d-preview" data-testid="bank-preview">
     <label htmlFor="bank-target">导入到</label>
     <select id="bank-target" data-testid="bank-target" value={targetBank} disabled={busy}
       onChange={e=>setTargetBank(e.target.value)}>
      <option value="">新建一个题库</option>
      {banks.filter(bank=>bank.origin==='private').map(bank=>
       <option key={bank.bank_id} value={`${bank.bank_id}|${bank.version}`}>
        {bank.name} · 当前 v{bank.version}（更新为新版本）
       </option>)}
     </select>
     <p><strong>{preview.document.name}</strong> · <span className="d-num d-num-sm">{preview.document.items.length}</span> 题</p>
     <ul>
      {preview.items_without_reference.length>0&&<li>没有参考答案：{preview.items_without_reference.join('、')}</li>}
      {preview.choice_items_without_key.length>0&&<li>选择题缺少正确答案：{preview.choice_items_without_key.join('、')}</li>}
      {preview.programming_items_require_validation.length>0&&<li>编程题需要额外校验：{preview.programming_items_require_validation.join('、')}</li>}
      <li>内容由你提供，系统未做正确性审核；导入只代表题目可用，不代表能力认证。</li>
     </ul>
     <div className="d-form-foot"><span className="fine-print">确认后会新建一个题库（v1）。</span>
      <button type="button" className="primary" disabled={busy} onClick={()=>void commit()}>确认导入</button></div>
    </div>}
   </>}
  </section>
 </div>;
}
