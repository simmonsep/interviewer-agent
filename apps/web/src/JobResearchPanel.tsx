import {useEffect,useRef,useState} from 'react';
import {api,post,ApiError,describeError} from './api.ts';
import {notifyDataChanged} from './dataEvents.ts';
import {getPositionBrief} from './positionBrief.ts';
import {PositionBriefCard} from './PositionBriefCard.tsx';

interface Check{field:string;status:string;hard:boolean;source_path:string;source_quote:string|null;user_ref:{goal_version:number;field:string;value:unknown};note:string}
interface Match{snapshot_id:string;outcome:string;checks:Check[];agent_reviewed:boolean}
interface RequestInfo{kind:string;keyword:string;source_id:string;job_id:string;
 // Present only on a self-added job: what the user typed, nothing fetched.
 company?:string;title?:string;cities?:string[];url?:string}
interface Run{id:string;goal_version:number;status:string;revision:number;stale:boolean;created_at:string;error_code:string|null;mode:'manual'|'agent';agent_origin:string|null;model_requests:number;source_requests:number;request:RequestInfo;result:{origin:string;readable:number;included:number;coverage:string;failures:{job_id?:string;status:string}[];search:{total:number|null;observed_at:string}|null;matches?:Match[];agent?:{action:string;question:string;read_job_ids:string[];tool_log:{tool:string;status:string}[];query_observations?:{keyword:string;status:string;job_ids:string[]}[]}|null;executed_request?:RequestInfo|null}|null}
interface Item{run_id:string;snapshot_id:string;source_id:string;source_job_id:string;source_url:string|null;content_sha256:string;observed_at:string;city_check:string;included:boolean;position_id?:string|null;position_version?:number|null;current_snapshot_id?:string|null;payload:{title:string;company:string|null;cities:string[];recruitment_label?:string|null;published_raw?:string|number|null;responsibilities?:string;requirements?:string;raw_text?:string;visible_text?:string;content_warnings:string[]}}
interface View{goal_version:number;goal_status:string;runs:Run[];items:Item[];selected?:{run_id:string;snapshot_id:string}|null;sources:{source_id:string;company:string;search:boolean;detail:boolean;coverage:string}[]}
type Binding={position_id:string;position_version:number;
 active:{resume_draft_id:string;resume_version:number;content_sha256:string}|null;
 history:{binding_id:string;resume_version:number;active:boolean;created_at:string}[];
 candidates:{draft_id:string;version:number;excerpt:string}[]}
const status:Record<string,string>={PENDING:'等待读取',RUNNING:'正在读取来源',SUCCEEDED:'读取完成',PARTIAL:'部分读取成功',FAILED:'读取未完成',CANCELLED:'已停止',WAITING_CREDENTIALS:'研究已暂停'};
const cityLabels:Record<string,string>={match:'符合已确认城市',conflict:'与已确认城市冲突',unknown:'城市待核验',not_requested:'未设置城市条件'};
const originLabels:Record<string,string>={source_live:'真实来源读取',offline_fixture:'合成来源演示',user_provided:'用户提供，未官网核验'};
// Source failures carry internal codes; say what happened without leaking them.
const failureLabels:Record<string,string>={rate_limited:'来源限流，请稍后再试',http_error:'来源返回错误',timeout:'来源响应超时',parse_error:'来源页面结构无法解析',blocked:'来源拒绝访问',unknown:'来源读取失败'};
function claimValue(field:string,value:unknown){
 if(value==null)return '未声明';
 // Some goal fields arrive as a JSON string of an array ("[\"上海市\"]"); show the
 // values, not the encoding, or the comparison table reads like a raw payload.
 if(typeof value==='string'&&value.trim().startsWith('[')){
  try{const parsed=JSON.parse(value);if(Array.isArray(parsed))return parsed.length?parsed.join('、'):'未声明';}catch{/* keep the original string */}
 }if(field==='recruitment')return ({intern:'实习',campus:'应届校招',experienced:'社会招聘',unknown:'尚未确认'}[String(value)]??String(value));if(Array.isArray(value))return value.length?value.join('、'):'未声明';return typeof value==='object'?JSON.stringify(value):String(value);}

export interface ResearchSelection{snapshotId:string;title:string;company:string|null;sourceId:string}
export function JobResearchPanel({userId,goalId,version,jdText,direction,cancelled,goalStatus,automatic,onGoalRefresh,onAuthLost,onSelection,applicationIntent,onOpenApplications}:{
 userId:string;goalId:string;version:number;jdText:string;direction:string;cancelled:boolean;goalStatus:string;automatic:boolean;
 onGoalRefresh:()=>void;onAuthLost:()=>void;
 /** The selected job is owned by the server; this only mirrors it into the shell context bar. */
 onSelection?:(value:ResearchSelection|null)=>void;
 /** F07Z-145: the user arrived from the review's "select a position first" entry. */
 applicationIntent?:boolean;
 /** F07Z-145: after a selection, offer the hand-off explicitly - never navigate on our own. */
 onOpenApplications?:()=>void;
}){
 const storageKey=`py-career:${userId}:research:${goalId}:${version}`;
 const [view,setView]=useState<View|null>(null),[keyword,setKeyword]=useState(direction),[source,setSource]=useState('tme-campus'),[jobId,setJobId]=useState(''),[error,setError]=useState(''),[busy,setBusy]=useState(false);
 const [ack,setAck]=useState<Record<string,boolean>>({});
 // Self-added job: the two built-in sources cannot reach most companies, so the
 // user can type one in. Nothing is fetched; every field is what they entered.
 const [company,setCompany]=useState(''),[title,setTitle]=useState(''),[manualCities,setManualCities]=useState(''),
  [manualUrl,setManualUrl]=useState(''),[manualJd,setManualJd]=useState('');
  // F07Z-152: editing a self-added JD keeps the position (and its resume binding) intact.
  const [editing,setEditing]=useState<{snapshotId:string;positionId:string;version:number;company:string;
   title:string;cities:string;url:string;jd:string}|null>(null);
  const [binding,setBinding]=useState<Binding|null>(null),[bindingDraft,setBindingDraft]=useState(''),[bindingBusy,setBindingBusy]=useState(false),[bindingNotice,setBindingNotice]=useState('');
 const alive=useRef(true),sequence=useRef(0),lock=useRef(false);
 const pending=useRef<{key:string;body:Record<string,unknown>}|null>(null);
 function failure(e:unknown){
  if(e instanceof ApiError&&e.status===401)onAuthLost();
  else if(e instanceof ApiError&&e.status===400)setError('查询参数不适用。小米当前只支持数字岗位ID详情；腾讯音乐支持关键词首屏查询。');
  else if(e instanceof ApiError&&e.status===409)setError('目标已更新、条件冲突、缺少确认或已有任务在进行，请核对后重试。');
  else setError(describeError(e));
 }
 function accept(value:View){
  if(value.goal_version!==version){setView(null);setError('目标已更新，请刷新上方求职目标后继续。');onSelection?.(null);return;}
  setView(value);
  const picked=value.selected?value.items.find(item=>item.snapshot_id===value.selected?.snapshot_id):null;
  onSelection?.(picked?{snapshotId:picked.snapshot_id,title:picked.payload.title,company:picked.payload.company,sourceId:picked.source_id}:null);
  if(value.goal_status!==goalStatus)onGoalRefresh();
 }
 async function refresh(){const n=++sequence.current;try{const result=await api<View>(`/api/career-goals/${goalId}/jobs`);if(alive.current&&n===sequence.current)accept(result);}catch(e){if(alive.current)failure(e);}}
 useEffect(()=>{
  alive.current=true;
  try{const saved=JSON.parse(sessionStorage.getItem(storageKey)??'null');if(saved?.key&&saved?.body){pending.current=saved;setKeyword(saved.body.keyword??direction);setSource(saved.body.source_id??'tme-campus');setJobId(saved.body.job_id??'');}}
  catch{sessionStorage.removeItem(storageKey);}
  void refresh();return()=>{alive.current=false;sequence.current++;};
 },[goalId,version]);
 useEffect(()=>{void refresh();},[goalStatus]);
 const active=view?.runs.some(r=>!r.stale&&['PENDING','RUNNING'].includes(r.status))??false;
 useEffect(()=>{if(!active)return;const timer=setInterval(()=>void refresh(),1000);return()=>clearInterval(timer);},[active]);
 async function submit(kind:'search'|'detail'|'import_jd'){
  if(lock.current)return;lock.current=true;setBusy(true);setError('');
  const body={expected_version:version,kind,source_id:kind==='import_jd'?'tme-campus':source,keyword:kind==='search'?keyword:'',job_id:kind==='detail'?jobId:''};
  if(!pending.current||JSON.stringify(pending.current.body)!==JSON.stringify(body))pending.current={key:crypto.randomUUID(),body};
  sessionStorage.setItem(storageKey,JSON.stringify(pending.current));
  const n=++sequence.current;
  try{
   const value=await post<View>(`/api/career-goals/${goalId}/research`,pending.current.body,pending.current.key);
   pending.current=null;sessionStorage.removeItem(storageKey);
   if(alive.current&&n===sequence.current)accept(value);
  }catch(e){if(alive.current){failure(e);if(e instanceof ApiError&&e.status===409){pending.current=null;sessionStorage.removeItem(storageKey);await refresh();}}}
  finally{lock.current=false;if(alive.current)setBusy(false);}
 }
 async function saveManual(){
  if(lock.current)return;lock.current=true;setBusy(true);setError('');
  const body={expected_version:version,company:company.trim(),title:title.trim(),
   cities:manualCities.split(/[,，、]/).map(x=>x.trim()).filter(Boolean).slice(0,5),
   recruitment_label:'',url:manualUrl.trim(),jd_text:manualJd.trim()};
  try{
   const value=await post<View>(`/api/career-goals/${goalId}/jobs/manual`,body,crypto.randomUUID());
   if(alive.current){accept(value);setCompany('');setTitle('');setManualCities('');setManualUrl('');setManualJd('');}
  }catch(e){if(alive.current)failure(e);}finally{lock.current=false;if(alive.current)setBusy(false);}
 }
 async function beginEdit(item:Item){
  setEditing({snapshotId:item.snapshot_id,positionId:item.position_id??'',version:item.position_version??1,
   company:item.payload.company??'',title:item.payload.title,cities:item.payload.cities.join('、'),
   url:item.source_url??'',jd:item.payload.raw_text??item.payload.visible_text??''});
 }
 async function saveEdit(){
  if(!editing||lock.current)return;lock.current=true;setBusy(true);setError('');
  const body={expected_version:editing.version,company:editing.company.trim(),title:editing.title.trim(),
   cities:editing.cities.split(/[,，、]/).map(x=>x.trim()).filter(Boolean).slice(0,5),
   recruitment_label:'',url:editing.url.trim(),jd_text:editing.jd.trim()};
  try{
   const value=await post<View>(`/api/career-goals/${goalId}/jobs/${editing.positionId}/revisions`,body,crypto.randomUUID());
   if(alive.current){accept(value);setEditing(null);}
  }catch(e){
   if(alive.current&&e instanceof ApiError&&e.code==='position_version_conflict')setError('这个岗位刚被更新过，请刷新后再改。');
   else if(alive.current)failure(e);
  }finally{lock.current=false;if(alive.current)setBusy(false);}
 }
 async function loadBinding(item:Item){
  if(!item.position_id)return;
  try{
   const value=await api<Binding>(`/api/career-goals/${goalId}/positions/${item.position_id}/binding`);
   setBinding(value);
   setBindingDraft(value.active?.resume_draft_id??value.candidates[0]?.draft_id??'');
  }catch(e){failure(e);}
 }
 async function saveBinding(item:Item){
  if(!binding||!item.position_id||!bindingDraft)return;
  setBindingBusy(true);setError('');
  try{
   const value=await api<Binding>(`/api/career-goals/${goalId}/positions/${item.position_id}/binding`,
    {method:'PUT',headers:{'Content-Type':'application/json','Idempotency-Key':crypto.randomUUID()},
     body:JSON.stringify({expected_version:binding.position_version,draft_id:bindingDraft})});
   setBinding(value);setBindingDraft(value.active?.resume_draft_id??bindingDraft);
   setBindingNotice('已换绑。已经生成的准备计划和面试仍用它们各自冻结的版本；想看新简历就先重新生成计划。');
  }catch(e){failure(e);}
  finally{setBindingBusy(false);}
 }
 async function select(item:Item){
  if(lock.current)return;lock.current=true;setBusy(true);setError('');
  try{const value=await post<View>(`/api/career-goals/${goalId}/selection`,{expected_version:version,run_id:item.run_id,snapshot_id:item.snapshot_id,acknowledge_unknown:ack[item.snapshot_id]??false});if(alive.current){accept(value);notifyDataChanged('job');}}
  catch(e){if(alive.current)failure(e);}finally{lock.current=false;if(alive.current)setBusy(false);}
 }
 const currentRun=view?.runs.find(r=>!r.stale);
 // Fetched results belong to the run that produced them, so only the current run's
 // items are shown. A self-added job has its own run and must not vanish the next
 // time someone searches, so those are always included and de-duplicated.
 const seen=new Set<string>();
 const items=(view?.items??[]).filter(x=>x.run_id===currentRun?.id
   ||(x.source_id==='user-jd'&&x.current_snapshot_id===x.snapshot_id))
  .filter(x=>{if(seen.has(x.snapshot_id))return false;seen.add(x.snapshot_id);return true;});
 const executed=currentRun?.result?.executed_request??(currentRun?.mode==='agent'?null:currentRun?.request);
 // F07Z-151: the brief always describes the SELECTED position, not whichever card is on screen.
 const briefItem=view?.selected?items.find(item=>item.snapshot_id===view.selected?.snapshot_id):undefined;
 const briefRun=briefItem?view?.runs.find(run=>run.id===briefItem.run_id):undefined;
 const briefMatch=briefItem
  ?briefRun?.result?.matches?.find(match=>match.snapshot_id===briefItem.snapshot_id)??null
  :null;
 const brief=briefItem?getPositionBrief({item:briefItem,match:briefMatch}):null;
 const disabled=busy||active||cancelled||(automatic&&['PENDING','RUNNING'].includes(goalStatus))||(view?.runs.some(r=>!r.stale&&r.status==='WAITING_CREDENTIALS')??false);
 return <section className="card job-research" aria-label="岗位来源与列表">
  <h2>岗位来源与列表</h2><p>先读取真实岗位，再比较要求。当前支持腾讯音乐关键词首屏最多5条，或按两来源的岗位ID读取详情；覆盖范围有限，在招状态待核验。</p>
  {view?.selected&&<p className="notice" role="status">已选择岗位用于后续准备；这不是投递或资格认证。该目标版本的选择已保存，修改目标后可重新选择。</p>}
  {brief&&<PositionBriefCard brief={brief}/>}
  {view?.selected&&applicationIntent&&onOpenApplications&&<div className="notice d-position-next" role="status"
   data-testid="position-to-application">
   <strong>岗位已选定</strong>
   <p>下一步可以记录真实投递。是否投递由你自己决定，这里不会自动跳转。</p>
   <button type="button" className="secondary" data-testid="position-to-application-cta"
    onClick={onOpenApplications}>记录一次投递</button>
  </div>}
  {error&&<p role="alert" className="error">{error}</p>}
  {automatic&&['PENDING','RUNNING'].includes(goalStatus)&&<p className="fine-print">正在协调自动研究，完成前暂不重复发起手动查询。</p>}
  <div className="career-fields"><label>岗位来源<select aria-label="岗位来源" value={source} onChange={e=>setSource(e.target.value)}><option value="tme-campus">腾讯音乐 · 校园门户</option><option value="xiaomi-intern">小米 · 实习详情</option></select></label>
   <label>查询关键词<input aria-label="查询关键词" maxLength={80} value={keyword} onChange={e=>setKeyword(e.target.value)}/></label></div>
  <p className="fine-print">{view?.sources.find(x=>x.source_id===source)?.coverage}</p>
  <button className="secondary" disabled={disabled||source!=='tme-campus'||!keyword.trim()} onClick={()=>void submit('search')}>查询岗位</button>
  <label>官网岗位ID<input aria-label="官网岗位ID" maxLength={24} value={jobId} onChange={e=>setJobId(e.target.value)} placeholder="从支持来源的详情地址取得数字ID"/></label>
  <button className="secondary" disabled={disabled||!jobId.trim()} onClick={()=>void submit('detail')}>读取岗位详情</button>
  <button className="text-button" disabled={disabled||!jdText.trim()} onClick={()=>void submit('import_jd')}>导入已保存的JD</button>
  {/* Escape hatch: the two built-in sources cover two companies, so any other job has
      to be entered by hand. It lands in the same list and goes through the same city
      and recruitment checks - it is marked as user-provided, not as verified. */}
  <details className="d-manual-job" data-testid="manual-job-form">
   <summary>自建岗位（上面的来源没有这家公司时）</summary>
   <div className="career-fields">
    <label>公司<input aria-label="自建岗位公司" maxLength={80} value={company} onChange={e=>setCompany(e.target.value)}/></label>
    <label>职位<input aria-label="自建岗位职位" maxLength={120} value={title} onChange={e=>setTitle(e.target.value)}/></label>
    <label>工作城市<input aria-label="自建岗位城市" maxLength={80} placeholder="多个用逗号分隔" value={manualCities} onChange={e=>setManualCities(e.target.value)}/></label>
    <label>官网链接<input aria-label="自建岗位链接" maxLength={500} placeholder="https://（可不填）" value={manualUrl} onChange={e=>setManualUrl(e.target.value)}/></label>
   </div>
   <label>JD 正文<textarea aria-label="自建岗位JD" rows={5} maxLength={20000} value={manualJd}
    onChange={e=>setManualJd(e.target.value)} placeholder="把招聘页面上的职责与要求粘贴进来"/></label>
   <button className="secondary" data-testid="manual-job-save"
    disabled={disabled||!company.trim()||!title.trim()||!manualJd.trim()} onClick={()=>void saveManual()}>保存为岗位</button>
   <p className="fine-print">保存后与抓取的岗位一样参与城市、招聘类型对照并可选岗；来源会标注为「用户提供，未官网核验」，不会被当作已核验的官网信息。</p>
  </details>
  <button className="text-button" disabled={busy} onClick={()=>void refresh()}>刷新岗位列表</button>
  {currentRun&&<div className="research-status" data-status={currentRun.status} role="status"><strong>{status[currentRun.status]??currentRun.status}</strong>
   <p>{currentRun.result?originLabels[currentRun.result.origin]:'请求已保存，等待后台处理'} · 目标版本 {currentRun.goal_version}</p>
   {currentRun.mode==='agent'&&<p>岗位研究 Agent · {currentRun.agent_origin==='real_model'?'真实模型':currentRun.agent_origin==='scripted_protocol'?'脚本Agent演示':'等待执行'} · 工具读取 {currentRun.source_requests} 次</p>}
   {executed&&<p>{executed.kind==='search'?`本次关键词：${executed.keyword}`:executed.kind==='detail'?`本次岗位ID：${executed.job_id}`:executed.kind==='manual'?`本次自建：${currentRun?.request?.company??''} · ${currentRun?.request?.title??''}`:'本次读取：目标中已保存的JD'}</p>}
   {currentRun.result?.agent?.question&&<p className="notice">研究需要补充：{currentRun.result.agent.question} 请在上方补充目标；等待时不会继续调用模型。</p>}
   {currentRun.result?.agent?.tool_log&&<p className="fine-print">研究动作：{currentRun.result.agent.tool_log.map(x=>({read_research_scope:'读取委派范围',search_jobs:'查询岗位',read_job:'查看详情',finish_research:'提交研究结果',ask_user:'请求补充'}[x.tool]??'拒绝未知工具')+(x.status==='error'?'（未通过校验）':'')).join(' → ')}</p>}
   {currentRun.result?.agent?.query_observations?.map((x,index)=><p className="fine-print" key={index}>查询 {index+1}：{x.keyword} · 来源状态 {x.status} · 返回 {x.job_ids.length} 个ID</p>)}
   {currentRun.result&&<><p>读取 {currentRun.result.readable} 条，保留 {currentRun.result.included} 条。{executed?.kind==='search'&&`本次仅查看首屏最多5条，来源报告总数：${currentRun.result.search?.total??'未知'}；不能据此判断其他页没有机会。`}</p>
    {currentRun.result.failures.length>0&&<p>未完成项：{currentRun.result.failures.map(x=>`${x.job_id??'列表'}：${failureLabels[x.status]??x.status}`).join('；')}。可改用支持的详情ID或导入JD。</p>}
    {executed&&currentRun.result.readable===0&&currentRun.status==='SUCCEEDED'&&<p>本次查询未返回可读取岗位，可调整关键词；不代表该城市没有岗位。</p>}</>}
   {currentRun.error_code&&<p>本次没有提交岗位结果，可重新发起读取。原因：{failureLabels[currentRun.error_code]??currentRun.error_code}</p>}
  </div>}
  {items.filter(x=>x.included).map(item=>{const runOf=view?.runs.find(r=>r.id===item.run_id);const match=runOf?.result?.matches?.find(x=>x.snapshot_id===item.snapshot_id);return <article className="job-card" key={item.snapshot_id}>
   <h3>{item.payload.title}</h3><p>{item.payload.company??'公司未核验'} · {item.payload.cities.join('、')||'工作城市未知'} · {item.payload.recruitment_label??'招聘类型未知'}</p>
   <p>{originLabels[runOf?.result?.origin??'']} · {cityLabels[item.city_check]} · 在招状态待核验</p>
   <p className="fine-print">观察：{new Date(item.observed_at).toLocaleString()}；来源发布日期：{item.payload.published_raw??'未知'}；内容版本 {item.content_sha256.slice(0,12)}</p>
   {item.source_url&&<a href={item.source_url} target="_blank" rel="noreferrer">查看官网原页</a>}
   {item.payload.content_warnings.length>0&&<p className="notice">原文含指令式内容，保留供核对；系统不将其作为操作指令。</p>}
   <details><summary>查看岗位正文与要求</summary>{item.payload.responsibilities&&<><h4>职责</h4><p className="job-text">{item.payload.responsibilities}</p><h4>要求</h4><p className="job-text">{item.payload.requirements}</p></>}{item.payload.visible_text&&<p className="job-text">{item.payload.visible_text}</p>}</details>
   {item.source_id==='user-jd'&&item.position_id&&
    <button type="button" className="text-button" disabled={busy} onClick={()=>beginEdit(item)}>编辑 JD</button>}
   {item.position_id&&<details className="d-job-binding" data-testid="job-binding"
    onToggle={event=>{if((event.target as HTMLDetailsElement).open)void loadBinding(item);}}>
    <summary>简历版本{binding?.position_id===item.position_id&&binding.active?` · v${binding.active.resume_version}`:''}</summary>
    {binding?.position_id===item.position_id&&<div>
     {binding.candidates.length===0
      ?<p className="fine-print">这个目标还没有可用的简历版本；先在材料页生成或导入一版。</p>
      :<>
       <label>使用版本<select aria-label="岗位简历版本" data-testid="job-binding-select"
        value={bindingDraft} onChange={e=>setBindingDraft(e.target.value)}>
        {binding.candidates.map(candidate=><option key={candidate.draft_id} value={candidate.draft_id}>
         v{candidate.version}{candidate.excerpt?` · ${candidate.excerpt}`:''}</option>)}
       </select></label>
       <button type="button" className="secondary" data-testid="job-binding-save"
        disabled={bindingBusy||!bindingDraft} onClick={()=>void saveBinding(item)}>换绑</button>
       <button type="button" className="text-button" data-testid="job-binding-refresh"
        disabled={bindingBusy} onClick={()=>void loadBinding(item)}>刷新版本</button>
      </>}
     <p className="fine-print">{binding.active
       ?`当前绑定 v${binding.active.resume_version}；之后生成的新简历不会自动替换它。换绑历史 ${binding.history.length} 条。`
       :'还没有绑定简历；新开场默认不带简历。'}</p>
      {bindingNotice&&<p className="fine-print" data-testid="job-binding-notice">{bindingNotice}</p>}
    </div>}
   </details>}
   {editing?.snapshotId===item.snapshot_id&&<div className="d-manual-job" data-testid="job-edit-form">
    <div className="career-fields">
     <label>公司<input aria-label="编辑岗位公司" maxLength={80} value={editing.company}
      onChange={e=>setEditing({...editing,company:e.target.value})}/></label>
     <label>职位<input aria-label="编辑岗位职位" maxLength={120} value={editing.title}
      onChange={e=>setEditing({...editing,title:e.target.value})}/></label>
     <label>工作城市<input aria-label="编辑岗位城市" maxLength={80} value={editing.cities}
      onChange={e=>setEditing({...editing,cities:e.target.value})}/></label>
     <label>官网链接<input aria-label="编辑岗位链接" maxLength={500} value={editing.url}
      onChange={e=>setEditing({...editing,url:e.target.value})}/></label>
    </div>
    <label>JD 正文<textarea aria-label="编辑岗位JD" rows={5} maxLength={20000} value={editing.jd}
     onChange={e=>setEditing({...editing,jd:e.target.value})}/></label>
    <div>
     <button type="button" className="secondary" data-testid="job-edit-save"
      disabled={busy||!editing.jd.trim()||!editing.company.trim()||!editing.title.trim()}
      onClick={()=>void saveEdit()}>保存修改</button>
     <button type="button" className="text-button" disabled={busy} onClick={()=>setEditing(null)}>取消</button>
    </div>
    <p className="fine-print">修改会追加为该岗位的新版本；岗位本身不变，已绑定的简历不受影响。</p>
   </div>}
   {match&&<div className="job-match"><h4>{match.outcome==='excluded'?'存在硬条件冲突':'可继续了解，仍有待核验信息'}</h4><p className="fine-print">个人信息来自本目标版本的确认字段；自述技能不等于独立能力验证。{runOf?.mode==='agent'&&(match.agent_reviewed?'研究Agent已打开该详情。':'本条由规则对照，研究Agent未单独打开详情。')}</p>
    <div className="match-scroll"><table><thead><tr><th>条件</th><th>结论</th><th>你的声明</th><th>岗位证据</th></tr></thead><tbody>{match.checks.map((c,index)=><tr key={index}>
     <td>{{cities:'城市',recruitment:'招聘类型',graduation_year:'毕业年份',days_per_week:'每周天数',duration_months:'实习月数',skills:'技能',availability:'在招状态'}[c.field]??c.field}{c.hard?'':'（偏好/参考）'}</td>
     <td>{{pass:'符合声明',conflict:'冲突',unknown:'缺证/待核验',not_requested:'未设置'}[c.status]??c.status}</td>
     <td>{claimValue(c.field,c.user_ref.value)}</td><td>{c.source_quote??'原文未提供可确认依据'}<small>{c.note}</small></td>
    </tr>)}</tbody></table></div>
    {match.outcome!=='excluded'&&!view?.selected&&<label className="check-label"><input type="checkbox" checked={ack[item.snapshot_id]??false} onChange={e=>setAck({...ack,[item.snapshot_id]:e.target.checked})}/>我知悉仍有待核验项，选岗仅用于后续准备</label>}
    <button className="secondary" disabled={busy||cancelled||Boolean(view?.selected)||match.outcome==='excluded'||(match.outcome==='needs_verification'&&!ack[item.snapshot_id])} onClick={()=>void select(item)}>选择该岗位</button>
   </div>}
  </article>})}
  {items.some(x=>!x.included)&&<details><summary>查看与硬性城市要求冲突的岗位</summary>{items.filter(x=>!x.included).map(x=><p key={x.snapshot_id}>{x.payload.title} · {x.payload.cities.join('、')} · 未纳入当前列表，原观察已保留。</p>)}</details>}
  {(view?.runs.length??0)>1&&<details><summary>查询历史</summary>{view?.runs.map(r=><p key={r.id}>{new Date(r.created_at).toLocaleString()} · 版本{r.goal_version} · {status[r.status]}{r.stale?' · 旧目标版本，未作为当前结果展示':''}</p>)}</details>}
 </section>;
}
