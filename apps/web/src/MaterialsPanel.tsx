import {useCallback,useEffect,useRef,useState} from 'react';
import {api,post,ApiError,describeError} from './api.ts';
import {getAsyncActionCopy} from './asyncActionCopy.ts';
import {OperationErrorDetails,errorDetailOf,type OperationErrorDetail} from './OperationErrorDetails.tsx';
import {notifyDataChanged} from './dataEvents.ts';
import {ResumeTimeline} from './ResumeTimeline.tsx';

interface Source{id:string;name:string;format:string;text:string;material_revision:number;content_sha256:string;
 file_reference:{preview_id:string;byte_length:number;metadata:{locations?:unknown[]}}|null}
interface Fact{id:string;source_id:string;kind:string;quote:string;start_char:number;end_char:number;verification:string}
interface Proposal{source_id:string;kind:string;quote:string;start_char:number;end_char:number;content_sha256:string}
interface RewriteItem{text:string;source_ids:string[];change_note:string;evidence_segments?:string[]}
interface Run{id:string;status:string;mode:string;goal_version:number;material_revision:number;revision:number;
 model_requests:number;origin:string|null;error_code:string|null;stale:boolean;
 result:{action?:string;proposals?:Proposal[];rewrite_items?:RewriteItem[];suggestions?:string[];question?:string|null;fact_ids?:string[];limit?:string}|null}
interface Draft{id:string;version:number;goal_version:number;material_revision:number;markdown:string;origin?:string;created_at?:string;stale:boolean;
 // S00Gd: per-bullet source chain, read from the stored payload by the API.
 provenance?:Array<{index:number;text:string;source_ids:string[];
  sources:Array<{id:string;name:string|null;content_sha256:string|null;material_revision:number|null}>;
  segments:Array<{id:string;text:string;block?:string|null}>;
  transformation:string;confirmation:string;
  finding:{code:string;family:string;term:string}|null}>;
 payload:{draft_style?:string;rewrite_items?:RewriteItem[];suggestions?:string[];candidate_status?:string;
  basics?:string[];education?:string[];experience?:string[];projects?:string[];skills?:string[];
  segments?:Array<{id:string;text:string;heading?:boolean;block?:string|null}>;
  omitted_segments?:Array<{segment_id:string;reason:string;note?:string}>;
  structured_map?:Record<string,string[]>;
  missing_sections?:string[];
  omission_audit?:Array<{segment_id:string;reason:string;audited:boolean;overlap:number|null;supported:boolean|null}>;
  unsupported_omissions?:string[];
  truncated_segment_ids?:string[];
  structure_note?:string;
  coverage?:{eligible:number;covered:number;omitted:number;unaccounted:number;missing_segment_ids:string[]}}}
interface View{goal_version:number;material_revision:number;sources:Source[];facts:Fact[];runs:Run[];drafts:Draft[]}
// F07Z-94: a parsed PDF/DOCX is inert until the user imports it.
interface FilePreview{preview_id:string;filename:string;format:string;byte_length:number;text:string;
 requires_import_confirmation:boolean;material_revision:number}

const runStatus:Record<string,string>={PENDING:'等待处理',RUNNING:'正在整理',SUCCEEDED:'已完成',FAILED:'未完成',CANCELLED:'已停止',WAITING_CREDENTIALS:'已暂停'};
const kinds:Record<string,string>={education:'教育经历',experience:'实习与经历',project:'项目',contribution:'个人贡献',result:'结果与验证',skill:'技能声明'};
const failureText:Record<string,string>={model_not_enabled:'材料处理尚未启用',model_credentials_unavailable:'本机未配置模型凭证',model_budget_exhausted:'调用次数或额度已达上限，已停止新请求',model_usage_unknown:'上一次调用的用量未知，已暂停以免重复计费',model_authorization_unavailable:'当前授权不可用（策略或额度不匹配）',material_analysis_stale:'目标或材料已更新，请重新分析',coordination_in_progress:'目标正在协调中，稍后再试',invalid_fact_confirmation:'所选条目已失效',insufficient_sources:'请先添加一段材料'};
// F07Z-115: every deterministic rejection the materials API can return, in the user's words.
// The old code mapped only three file codes and collapsed everything else into one sentence,
// so a rejected PDF said "内容不符合要求" without saying which rule failed.
const materialErrorText:Record<string,string>={
 material_file_size_limit:'文件超过 2 MB，请压缩或换一份。',
 request_too_large:'提交内容过大：文件上限 2 MB，粘贴文本最多 20000 字，请删减后重试。',
 origin_not_allowed:'请求来源不受信任，请从应用内重新操作。',
 invalid_response:'服务返回了无法识别的内容，请重试；若持续出现，请重启应用。',
 material_file_type_mismatch:'文件内容与扩展名不一致（例如把 Word 文档改名成 .pdf），请换原文件再试。',
 material_file_invalid:'文件无法解析：结构损坏或不是有效的 PDF/DOCX。',
 material_pdf_encrypted:'PDF 有密码，先解除密码再导入。',
 material_pdf_page_limit:'PDF 页数超过上限（10 页），请拆分后再导入。',
 material_pdf_content_limit:'PDF 内容过多，请拆分后再导入。',
 material_file_text_unreadable:'读到了文件，但提取不出文字（可能是扫描件、特殊字体或受保护内容）。',
 material_file_text_limit:'提取的文字超过 20000 字上限，请删减后再导入。',
 material_file_no_text:'这个文件里没有读到文字；扫描版 PDF 需要先做 OCR。',
 material_docx_unsupported_content:'DOCX 含暂不支持的内容（图片、文本框、宏等），请另存为纯文字后再导入。',
 material_docx_archive_limit:'DOCX 压缩包异常（文件可能被伪装或损坏）。',
 material_docx_xml_limit:'DOCX 内部结构过大，请精简后再导入。',
 material_docx_paragraph_limit:'DOCX 段落过多，请删减后再导入。',
 unsupported_material_format:'只支持 .pdf 和 .docx。',
 material_file_parse_timeout:'文件解析超时，换成文本粘贴或更小的文件再试。',
 material_file_parser_unavailable:'文件解析器暂时不可用，请稍后重试。',
 material_file_preview_limit:'这个目标的文件预览已达 20 份上限，请先导入或清理。',
 material_file_preview_stale:'这份预览已过期（目标或材料更新），请重新导入。',
 material_scope_too_large:'材料总字数超过 20000，请拆分或删减后再试。',
 material_revision_limit:'这个目标的材料版本已达上限（30），请新建目标。',
 material_run_limit:'这个目标的材料任务次数已达上限（30），请新建目标。',
 rewrite_requires_draft:'改写只能以草稿模式发起，请刷新页面重试。',
 fact_scope_too_large_select_ids:'选择的事实过多，请分批确认。',
 resume_item_not_available:'所选简历条目已失效，请重新选择。',
 resume_scope_too_large_select_items:'选择的简历条目过多（最多 6 条）。',
 invalid_revision_items:'草稿修改内容不合法，请检查后重试。',
 sections_require_edit:'只有编辑操作可以带段落修改。',
 suggestions_require_edit:'只有编辑操作可以选择建议。',
 suggestion_not_available:'所选建议已不存在，请刷新后重试。',
 draft_version_limit:'这份草稿的版本已达上限（100），请新建一份。',
 invalid_request:'文件名过长（最多 80 字符）或页面状态已变化，请重命名/刷新后重试。',
};
// F07Z-104: which fuse stopped the run. The error code stays `model_budget_exhausted`; this
// names the limit so the user knows whether to open a new authorization or a new goal.
const limitText:Record<string,string>={authorization_http_limit:'这张授权的历史调用次数已用完（旧记录）；提高材料授权总额会新建一笔授权，旧花费不结转',authorization_nano_limit:'余额不够下一次预留：每次调用要先预留约 ¥0.15。提高材料授权总额可继续——改数字会新建一笔授权，旧花费不结转，新上限也不叠加在旧余额上',goal_budget_limit:'这个目标的模型调用预算已用完，请换一个目标或新开授权',goal_material_request_limit:'这个目标的材料调用次数（72 次）已用完，请新开一个目标',run_operation_attempt_limit:'这一次运行的尝试次数（12 次）已用完，请重新发起一次运行'};

// The four fixed sections render from code, so they are shown and corrected here
// rather than buried in the exported markdown.
type SectionKey='basics'|'education'|'experience'|'projects'|'skills';
const sectionOrder:[SectionKey,string][]=[['basics','基本信息'],['education','教育经历'],['experience','工作经历'],['projects','项目经历'],['skills','技能']];
const sectionLabel:Record<string,string>={basics:'基本信息',education:'教育经历',experience:'工作经历',projects:'项目经历',skills:'技能'};
const reasonLabel:Record<string,string>={duplicate:'重复',merged_elsewhere:'声称已合并',low_relevance:'相关性低',structural_noise:'结构噪声',user_excluded:'用户排除',other:'其他'};
const noSections:Record<SectionKey,string[]>={basics:[],education:[],experience:[],projects:[],skills:[]};

export function MaterialsPanel({goalId,version,goalStatus,cancelled,resumeFocus=false,onAuthLost,onGoalRefresh,onStartInterview,goalTitle,jobKeyPoints}:{
 goalId:string;version:number;goalStatus:string;cancelled:boolean;
 // Version B: entering from the "简历修改 Agent" interface pins this panel to
 // the real rewrite endpoint. It does not change any other materials action.
 resumeFocus?:boolean;onAuthLost:()=>void;onGoalRefresh:()=>void;
 /** F07Z-142: the draft is the end of this page; offer the next step where it is produced. */
 onStartInterview?:()=>void;
 /** F07Z-143: the confirmed target name, so this page is not context-free. */
 goalTitle?:string|null;
 /** F07Z-151: key words from the selected position's JD (same brief the research page shows). */
 jobKeyPoints?:{title:string;skills:string[]}|null;
}){
 const [view,setView]=useState<View|null>(null),[error,setError]=useState(''),[busy,setBusy]=useState(false),[notice,setNotice]=useState('');
 // F07Z-146: the technical code is kept apart from the human sentence and shown collapsed.
 const [errorDetail,setErrorDetail]=useState<OperationErrorDetail|null>(null);
 const [generating,setGenerating]=useState(false);
 const [name,setName]=useState('简历片段'),[format,setFormat]=useState<'text'|'markdown'>('markdown'),[text,setText]=useState('');
 const [picked,setPicked]=useState<number[]>([]),[draftTexts,setDraftTexts]=useState<string[]>([]);
 const [pickedSuggestions,setPickedSuggestions]=useState<number[]>([]);
 // F07Z-144: only this button's own request may switch it to "正在保存…".
 const [savingSource,setSavingSource]=useState(false);
 const [sections,setSections]=useState<Record<SectionKey,string[]>>(noSections);
 const [filePreview,setFilePreview]=useState<FilePreview|null>(null),[fileBusy,setFileBusy]=useState(false);
 const [materialLive,setMaterialLive]=useState(false);
 const [importing,setImporting]=useState(false);
 const [restoring,setRestoring]=useState(false);
 const [historyAll,setHistoryAll]=useState(false);
 const alive=useRef(true),sequence=useRef(0),lock=useRef(false),addBox=useRef<HTMLDetailsElement|null>(null),
  fileInput=useRef<HTMLInputElement|null>(null);

 // F07Z-112: the materials worker may run live when the user enabled that separate switch.
 // Reading the runtime flag keeps the copy truthful; if the read fails we keep the scripted
 // wording, which is the shipped default.
 useEffect(()=>{
  let mounted=true;
  void (async()=>{
   try{
    const value=await api<{material_active:boolean}>('/api/runtime/model-settings');
    if(mounted)setMaterialLive(!!value.material_active);
   }catch{/* keep the scripted wording */}
  })();
  return()=>{mounted=false;};
 },[]);

 const accept=useCallback((value:View)=>{
  setView(value);
  if(value.goal_version!==version){setError('目标已更新，请刷新后继续。');onGoalRefresh();}
 },[version,onGoalRefresh]);

 const refresh=useCallback(async()=>{
  const n=++sequence.current;
  try{const value=await api<View>(`/api/career-goals/${goalId}/materials`);if(alive.current&&n===sequence.current)accept(value);}
  catch(e){if(alive.current){if(e instanceof ApiError&&e.status===401)onAuthLost();else setError(describeError(e));}}
 },[goalId,accept,onAuthLost]);

 useEffect(()=>{
  alive.current=true;void refresh();
  return()=>{alive.current=false;sequence.current++;};
 },[refresh]);

 async function importResume(){
  if(importing||!view)return;
  setImporting(true);setError('');setNotice('');
  try{
   const value=await post<View&{version:number}>(`/api/career-goals/${goalId}/materials/resume-imports`,
    {expected_version:version,expected_revision:view.material_revision},crypto.randomUUID());
   setView(value);
   setNotice(`已把上传的原文导入为简历 v${value.version}；这一步不调用模型，也不新增事实。`);
  }catch(e){if(e instanceof ApiError&&e.status===401){onAuthLost();return;}failure(e);}
  finally{setImporting(false);}
 }
 async function restoreDraft(draft:Draft){
  if(restoring||!view)return;
  setRestoring(true);setError('');setNotice('');
  try{
   const value=await post<View>(`/api/career-goals/${goalId}/materials/drafts/${draft.id}/restore`,
    {expected_version:version,expected_revision:view.material_revision},crypto.randomUUID());
   setView(value);
   setNotice(`已把 v${draft.version} 恢复为一个新版本；旧版本仍保留。`);
  }catch(e){if(e instanceof ApiError&&e.status===401){onAuthLost();return;}failure(e);}
  finally{setRestoring(false);}
 }
 const active=view?.runs.some(run=>!run.stale&&['PENDING','RUNNING'].includes(run.status))??false;
 useEffect(()=>{if(!active)return;const timer=setInterval(()=>void refresh(),1000);return()=>clearInterval(timer);},[active,refresh]);

 const currentRun=view?.runs.find(run=>!run.stale)??null;
 const latestDraft=view?.drafts.find(draft=>!draft.stale)??null;
 const rewriteItems=latestDraft?.payload.draft_style==='rewrite'?(latestDraft.payload.rewrite_items??[]):[];
 const draftSuggestions:string[]=(latestDraft?.payload.draft_style==='rewrite'?(latestDraft.payload.suggestions??[]):[]);
 const analysisRun=(view?.runs??[]).find(run=>run.status==='SUCCEEDED'&&run.result?.action==='analysis'&&!run.stale)??null;
 const proposals=analysisRun?.result?.proposals??[];
 // Where each source line ended up; computed from the ledger the backend already stores.
 const structuredMap=latestDraft?.payload.structured_map??{};
 const idSection=new Map<string,string>();
 Object.entries(structuredMap).forEach(([key,ids])=>(ids??[]).forEach(id=>idSection.set(id,sectionLabel[key]??key)));
 const idItem=new Map<string,number>();
 rewriteItems.forEach((item,index)=>(item.evidence_segments??[]).forEach(id=>{if(!idItem.has(id))idItem.set(id,index+1);}));
 const idOmitted=new Map<string,string>();
 (latestDraft?.payload.omitted_segments??[]).forEach(entry=>idOmitted.set(entry.segment_id,entry.reason));

 useEffect(()=>{setPicked([]);},[analysisRun?.id]);
 useEffect(()=>{setDraftTexts(rewriteItems.map(item=>item.text));setPickedSuggestions([]);
  setSections({basics:latestDraft?.payload.basics??[],education:latestDraft?.payload.education??[],experience:latestDraft?.payload.experience??[],
   projects:latestDraft?.payload.projects??[],skills:latestDraft?.payload.skills??[]});},[latestDraft?.id]);

 function failure(e:unknown,fallback?:string){
  setErrorDetail(errorDetailOf(e));
  if(e instanceof ApiError&&e.status===401){onAuthLost();return;}
  if(e instanceof ApiError&&materialErrorText[e.code]){setError(materialErrorText[e.code]);return;}
  if(e instanceof ApiError&&e.status===409){setError('目标或材料已更新，已重新读取最新状态。');void refresh();return;}
  if(e instanceof ApiError&&e.status===400){setError(`请求未通过校验（${e.code}），请刷新后重试。`);return;}
  // F07Z-146 §39: name the failed action when the caller knows it; otherwise keep the generic
  // "input is still here" sentence.
  setError(fallback??describeError(e));
 }
 async function run<T>(action:()=>Promise<T>,after?:()=>void,fallback?:string){
  if(lock.current)return;lock.current=true;setBusy(true);setError('');setNotice('');setErrorDetail(null);
  try{await action();after?.();notifyDataChanged('materials');}
  catch(e){failure(e,fallback);}
  finally{lock.current=false;if(alive.current)setBusy(false);}
 }

 // F07Z-94: upload the file as-is and let the backend parse it in a child process; the
 // preview is shown before anything enters the material ledger.
 const uploadFile=async(event:React.ChangeEvent<HTMLInputElement>)=>{
  const file=event.target.files?.[0];event.target.value='';
  if(!file||!view)return;
  setFileBusy(true);setError('');setNotice('');
  try{
   const docx=file.name.toLowerCase().endsWith('.docx');
   const value=await api<FilePreview>(
    `/api/career-goals/${goalId}/materials/file-previews?filename=${encodeURIComponent(file.name)}`+
    `&expected_version=${version}&expected_revision=${view.material_revision}`,
    {method:'POST',body:file,headers:{'Content-Type':docx
     ?'application/vnd.openxmlformats-officedocument.wordprocessingml.document':'application/pdf',
     'Idempotency-Key':crypto.randomUUID()}});
   if(alive.current)setFilePreview(value);
  }catch(e){
   if(e instanceof ApiError&&e.status===401){onAuthLost();return;}
   if(e instanceof ApiError&&materialErrorText[e.code])setError(materialErrorText[e.code]);
   else failure(e);
  }finally{if(alive.current)setFileBusy(false);}
 };

 const importPreview=()=>run(async()=>{
  if(!view||!filePreview)return;
  const value=await post<View>(
   `/api/career-goals/${goalId}/materials/file-previews/${filePreview.preview_id}/import`,
   {expected_version:version,expected_revision:view.material_revision},crypto.randomUUID());
  const filename=filePreview.filename;
  accept(value);setFilePreview(null);
  const copy=getAsyncActionCopy('material-import','success',{name:filename});
  setNotice(`${copy.feedbackTitle}。${copy.feedbackBody}`);
 },undefined,getAsyncActionCopy('material-import','error').feedbackBody);

 const addSource=()=>{
  const copy=getAsyncActionCopy('material-save','success',{name});
  setSavingSource(true);
  return run(async()=>{
   if(!view)return;
   const value=await post<View>(`/api/career-goals/${goalId}/materials/sources`,
    {expected_version:version,expected_revision:view.material_revision,name:name.trim(),format,text},crypto.randomUUID());
   accept(value);setText('');
   setNotice(`${copy.feedbackTitle}。${copy.feedbackBody}`);
  },undefined,getAsyncActionCopy('material-save','error').feedbackBody).finally(()=>{if(alive.current)setSavingSource(false);});
 };

 const startRun=(mode:'analyze'|'draft'|'rewrite')=>{
  if(mode==='draft')setGenerating(true);
  return run(async()=>{
  if(!view)return;
  const url=mode==='rewrite'?`/api/career-goals/${goalId}/materials/rewrite`:`/api/career-goals/${goalId}/materials/runs`;
  const body=mode==='rewrite'
   ?{expected_version:version,expected_revision:view.material_revision}
   :{expected_version:version,expected_revision:view.material_revision,mode,...(mode==='draft'?{draft_style:'source'}:{})};
  const value=await post<View>(url,body,crypto.randomUUID());
  accept(value);
  }).finally(()=>{if(alive.current)setGenerating(false);});
 };

 const confirmPicked=()=>run(async()=>{
  if(!view||!analysisRun||picked.length===0)return;
  const value=await post<View>(`/api/career-goals/${goalId}/materials/confirmations`,
   {expected_version:version,expected_revision:view.material_revision,run_id:analysisRun.id,proposal_ids:picked,continue_after_confirmation:false},
   crypto.randomUUID());
  accept(value);setPicked([]);setNotice('已确认这些条目，可以作为初稿依据。');
 });

 const saveDraft=()=>run(async()=>{
  if(!view||!latestDraft)return;
  const items=rewriteItems.map((item,index)=>({...item,text:draftTexts[index]??item.text}));
  const lines=(key:SectionKey)=>sections[key].map(line=>line.trim()).filter(line=>line.length>0);
  const value=await post<View>(`/api/career-goals/${goalId}/materials/drafts/${latestDraft.id}/revisions`,
   {expected_version:version,expected_revision:view.material_revision,expected_draft_version:latestDraft.version,action:'edit',items,
    selected_suggestion_indexes:pickedSuggestions,basics:lines('basics'),education:lines('education'),experience:lines('experience'),
    projects:lines('projects'),skills:lines('skills')},
   crypto.randomUUID());
  accept(value);setNotice('已保存为新版本，旧版本仍保留。');
 });

 if(!view)return <section className="card d-card" aria-label="材料与简历"><p className="muted">{error||'正在读取材料…'}</p></section>;

 return <div className="d-materials">
  {/* F07Z-142 §16-17: state the completion criterion and the count in words, not a bare digit
      in a card header. */}
  <header className="d-pane-head" data-testid="materials-head">
   <h1>材料与简历</h1>
   {goalTitle&&<p className="d-pane-goal" data-testid="materials-goal">{goalTitle}</p>}
   {jobKeyPoints&&<p className="fine-print d-job-points" data-testid="materials-job-points">
    岗位重点（来自岗位原文）：{jobKeyPoints.skills.length?jobKeyPoints.skills.join(' · '):'JD 中没有匹配到已知技能词'}</p>}
   <p>{view.sources.length===0
    ? '先把真实经历放进来。后续简历修改只会基于你确认过的内容。'
    : `已保存 ${view.sources.length} 份材料。后续简历修改只会基于这些内容。`}</p>
  </header>
  <ResumeTimeline goalId={goalId}/>
  {resumeFocus&&<div className="d-agent-banner" data-testid="resume-agent-banner">
    <div className="d-agent-banner-head"><span className="d-agent-badge" aria-hidden="true">AI</span>
     <strong>简历修改 Agent</strong><span className="tag">按材料改写</span></div>
    <p className="fine-print">只改写你已上传的原文，不会补写没发生的事；产出是初稿，需要你逐句审阅。</p>
    <div className="d-agent-banner-actions">
     <button type="button" className="primary" data-testid="resume-agent-run"
      disabled={busy||cancelled||active||view.sources.length===0} onClick={()=>void startRun('rewrite')}>运行简历修改 Agent</button>
     <span className="fine-print">{view.sources.length===0?'请先添加一段材料；没有材料时不会伪造改写。':active?'已有处理中的任务；不会重复提交。':'读取已上传材料原文，产出初稿与补充建议。'}</span>
    </div>
   </div>}
  {error&&<div className="d-error-block"><p role="alert" className="error">{error}</p>
   <OperationErrorDetails detail={errorDetail}/></div>}
  {notice&&<p className="notice" role="status">{notice}</p>}
  {cancelled&&<p className="notice">当前目标已结束，材料只读。</p>}

  <section className="card d-card" aria-label="材料来源">
   <div className="d-card-head"><h2>材料来源</h2><span className="n d-num d-num-sm">{view.sources.length}</span></div>
   <p className="muted">先把真实经历放进来。分析只从原文取句，不会替你编造经历。</p>
   {view.sources.length===0?<p className="fine-print">还没有材料。粘贴一段项目或实习经历即可开始。</p>:
    <div className="d-source-list">{view.sources.map(source=><div className="d-source" key={source.id}>
     <div className="d-source-head"><strong>{source.name}</strong>
      <span className="tag">{source.format==='markdown'?'Markdown':'纯文本'}</span>
      <span className="d-num d-num-sm">{source.text.length} 字</span>
      {source.file_reference&&<span className="tag">文件导入</span>}</div>
     <p className="d-source-body">{source.text.slice(0,140)}{source.text.length>140?'…':''}</p>
    </div>)}</div>}
   {!cancelled&&<div className="d-file-import">
    <input ref={fileInput} type="file" accept=".pdf,.docx" hidden aria-label="选择简历文件"
     onChange={event=>void uploadFile(event)}/>
    <button type="button" className="secondary" disabled={busy||fileBusy}
     onClick={()=>fileInput.current?.click()}>从 PDF / DOCX 导入</button>
    <span className="fine-print">最多 2 MB；只提取文字，扫描件读不出来。</span>
   </div>}
   {filePreview&&<div className="d-file-preview" data-testid="file-preview">
    <div className="d-file-preview-head"><strong>{filePreview.filename}</strong>
     <span className="fine-print">{Math.max(1,Math.round(filePreview.byte_length/1024))} KB · 提取 {filePreview.text.length} 字</span></div>
    <pre className="d-file-preview-text">{filePreview.text.slice(0,1200)}{filePreview.text.length>1200?'…':''}</pre>
    <div className="d-form-foot">
     <button type="button" className="text-button" disabled={fileBusy} onClick={()=>setFilePreview(null)}>放弃</button>
     <button type="button" className="primary" disabled={fileBusy} onClick={()=>void importPreview()}>导入为材料</button>
    </div>
   </div>}
   <details className="d-source-add" ref={addBox} open={view.sources.length===0}>
    <summary>添加一段材料</summary>
    <p className="muted">可以直接粘贴详细的经历流水账（最多 20000 字）；改写时 Agent 会把同一经历的多行细节合并精简为简历条目。</p>
    <div className="career-fields">
     <label>名称<input maxLength={80} value={name} onChange={e=>setName(e.target.value)}/></label>
     <label>格式<select value={format} onChange={e=>setFormat(e.target.value as 'text'|'markdown')}><option value="markdown">Markdown</option><option value="text">纯文本</option></select></label>
    </div>
    <label>正文<textarea aria-label="材料正文" maxLength={20000} value={text} onChange={e=>setText(e.target.value)} placeholder="例如：在实习中负责订单查询接口，用 Java 与 MyBatis 重写慢查询，P95 从 1.2s 降到 300ms。"/></label>
    <div className="d-form-foot"><span className="muted d-num d-num-sm">{text.length} / 20000</span>
     <button type="button" className="primary" disabled={busy||cancelled||!text.trim()||!name.trim()} onClick={()=>void addSource()}>{savingSource
      ?getAsyncActionCopy('material-save','submitting').buttonLabel
      :getAsyncActionCopy('material-save','idle').buttonLabel}</button></div>
   </details>
  </section>

  <section className="card d-card" aria-label="材料处理">
   <div className="d-card-head"><h2>分析与初稿</h2></div>
   <p className="muted">{materialLive
    ? '「按材料改写」会调用你配置的真实模型，按实际用量记账；初稿由模型生成，事实与措辞仍以原文为准。不需要先点「分析材料」。'
    : '分析会从材料里挑出可引用的句子，你确认后才进入初稿；「按材料改写」直接读取已导入原文生成完整初稿，不需要先点「分析材料」。初稿由剧本流程生成，措辞不代表模型效果。'}</p>
   <div className="d-run-actions">
    <button type="button" className="secondary" disabled={busy||cancelled||active||view.sources.length===0} onClick={()=>void startRun('analyze')}>分析材料</button>
    <button type="button" className="secondary" disabled={busy||cancelled||active||view.facts.length===0} onClick={()=>void startRun('draft')}>{generating
     ?getAsyncActionCopy('resume-generate','submitting').buttonLabel
     :'生成初稿'}</button>
    <button type="button" className="secondary" disabled={busy||cancelled||active||view.sources.length===0} onClick={()=>void startRun('rewrite')}>按材料改写</button>
   </div>
   {currentRun&&<p className="d-run-line" data-status={currentRun.status} role="status">
    <strong>{runStatus[currentRun.status]??currentRun.status}</strong>
    <span>· {currentRun.origin==='scripted_protocol'?'脚本流程':'模型'} · 目标版本 {currentRun.goal_version}</span>
    {(currentRun.status==='FAILED'||currentRun.status==='WAITING_CREDENTIALS')&&<span className="d-run-err">原因：{limitText[currentRun.result?.limit??'']??failureText[currentRun.error_code??'']??currentRun.error_code??'处理未完成'}</span>}
    {currentRun.result?.question&&<span className="d-run-err">需要补充：{currentRun.result.question}</span>}
   </p>}
  </section>

  {proposals.length>0&&<section className="card d-card" aria-label="分析建议">
   <div className="d-card-head"><h2>待确认的条目</h2><span className="n d-num d-num-sm">{picked.length}/{proposals.length}</span></div>
   <p className="muted">这些是从原文原样取出的句子。勾选表示你确认它可以作为简历依据。</p>
   <div className="d-proposals">{proposals.map((proposal,index)=><label className="d-proposal" key={`${proposal.source_id}-${index}`}>
    <input type="checkbox" checked={picked.includes(index)} disabled={busy||cancelled}
     onChange={e=>setPicked(e.target.checked?[...picked,index].sort((a,b)=>a-b):picked.filter(item=>item!==index))}/>
    <span className="d-proposal-body"><span className="tag">{kinds[proposal.kind]??proposal.kind}</span>{proposal.quote}</span>
   </label>)}</div>
   <div className="d-form-foot"><span className="muted">确认后写入「已确认依据」，可被初稿引用。</span>
    <button type="button" className="primary" disabled={busy||cancelled||picked.length===0} onClick={()=>void confirmPicked()}>确认所选</button></div>
  </section>}

  {view.facts.length>0&&<section className="card d-card" aria-label="已确认依据">
   <div className="d-card-head"><h2>已确认依据</h2><span className="n d-num d-num-sm">{view.facts.length}</span></div>
   <ul className="d-facts">{view.facts.map(fact=><li key={fact.id}><span className="tag">{kinds[fact.kind]??fact.kind}</span>{fact.quote}</li>)}</ul>
  </section>}

  {view&&view.sources.length>0&&<div className="d-set-actions">
   <button type="button" className="secondary" data-testid="resume-import" disabled={importing||busy}
    onClick={()=>void importResume()}>{importing?'正在导入…':'导入为简历（原文，0 调用）'}</button>
   <span className="fine-print">把已上传的原文作为简历版本；不调用模型，也不新增事实。</span>
  </div>}
  {latestDraft&&<div className="d-draft-done" data-testid="draft-complete">
   <div>
    <strong>{latestDraft.payload.draft_style==='verbatim'?'已按原文导入为简历':'简历初稿已生成'}</strong>
    <p className="fine-print">基于 {view.sources.length} 份已保存材料；没有添加未确认的经历。可以逐句审阅，或直接去练一场。</p>
    {latestDraft.payload.draft_style==='verbatim'&&<p className="fine-print" data-testid="draft-verbatim-scope">
      绑定到岗位后，面试只会带这份原文的前 6 条作为参考，仍标记为“你选用的简历”而不是已确认事实；
      想让更多条目参与，先用 Agent 改写成可选条目的草稿。</p>}
   </div>
   {onStartInterview&&!cancelled&&<button type="button" className="secondary" data-testid="draft-next-interview"
    onClick={onStartInterview}>开始模拟面试</button>}
  </div>}

  {latestDraft&&<section className="card d-card" aria-label="草稿">
   <div className="d-card-head"><h2>{latestDraft.payload.draft_style==='verbatim'?'简历（原文导入）':'草稿'} · 第 {latestDraft.version} 版</h2>
    <a className="text-button" href={`/api/career-goals/${goalId}/materials/drafts/${latestDraft.id}.md`} download={`draft-${latestDraft.version}.md`}>导出 Markdown</a></div>
   <details className="d-draft-history" data-testid="draft-history">
    <summary>历史版本（{view.drafts.length}）</summary>
    {(historyAll?view.drafts:view.drafts.slice(0,4)).map(draft=><p className="fine-print" key={draft.id} data-testid={`draft-version-${draft.version}`}>
     v{draft.version} · {draft.origin==='uploaded'?'原文导入':draft.origin==='restore'?'恢复':'模型/人工'}
     {draft.created_at?` · ${new Date(draft.created_at).toLocaleString()}`:''}
     {draft.version!==latestDraft.version&&<button type="button" className="text-button" disabled={restoring}
      data-testid={`restore-draft-${draft.version}`} onClick={()=>void restoreDraft(draft)}>恢复此版</button>}
    </p>)}
    {view.drafts.length>4&&<button type="button" className="text-button" data-testid="draft-history-more"
     onClick={()=>setHistoryAll(!historyAll)}>{historyAll?'收起较早版本':`显示全部 ${view.drafts.length} 版`}</button>}
   </details>
   {latestDraft.stale&&<p className="notice">目标或材料已更新，这份草稿是旧版本，仅作参考。</p>}
   {(latestDraft.payload.truncated_segment_ids?.length??0)>0&&<p className="notice" data-testid="draft-truncated">
     材料过长：本次有 {latestDraft.payload.truncated_segment_ids!.length} 段未纳入这份草稿（已处理的部分正常生成）。如需完整覆盖，请按经历分几次上传。
    </p>}
   {latestDraft.payload.structure_note==='heading_less'&&<p className="notice" data-testid="draft-heading-less">
     原文没有“工作经历 / 项目经历 / 教育经历 / 技能”这类小标题，所以这份草稿只按要点汇总，没有分栏。
     想要分栏版本：在材料里加上这几个小标题（或写成“教育经历：……”），再改写一次。
    </p>}
    {latestDraft.payload.coverage&&<div className="d-coverage" data-testid="draft-coverage"
     data-unaccounted={latestDraft.payload.coverage.unaccounted}
     data-unsupported={(latestDraft.payload.unsupported_omissions??[]).length}>
    <span className="fine-print">原文叙述段 {latestDraft.payload.coverage.eligible} 段：
     已写 {latestDraft.payload.coverage.covered} 段，明确不写 {latestDraft.payload.coverage.omitted} 段，
     {latestDraft.payload.coverage.unaccounted>0?`未说明 ${latestDraft.payload.coverage.unaccounted} 段`:'全部有去处'}。</span>
    {latestDraft.payload.coverage.unaccounted>0&&<p className="notice" role="status">
     这份初稿不完整：下面这些原文内容既没有写进正文，也没有说明为什么不写。
     {(latestDraft.payload.coverage.missing_segment_ids??[]).slice(0,5).map(id=>{
      const segment=latestDraft.payload.segments?.find(entry=>entry.id===id);
      return <span className="d-coverage-missing" key={id}>{segment?segment.text:`（段落 ${id}）`}</span>;
     })}
     {latestDraft.payload.coverage.unaccounted>5&&<span className="fine-print">等 {latestDraft.payload.coverage.unaccounted} 段</span>}
    </p>}
    {(latestDraft.payload.missing_sections??[]).length>0&&<p className="notice" data-testid="draft-missing-sections" role="status">
     {(latestDraft.payload.missing_sections??[]).length===4
      ?'你的材料没有任何分段标题（教育经历/工作经历/项目经历/技能）。缺段不等于没有经历——按段落标题补一段，我就能按同样的规则（只取原文、不编造）整理进去。'
      :`你的材料里没有这些段落：${(latestDraft.payload.missing_sections??[]).map(key=>sectionLabel[key]??key).join('、')}。缺段不等于没有经历——如果有，补一段原文，我按同样的规则（只取原文、不编造）整理进去。`}
     <button type="button" className="text-button" disabled={busy||cancelled} onClick={()=>{
      if(addBox.current){addBox.current.open=true;addBox.current.scrollIntoView({block:'center'});}
     }}>补一段材料</button>
    </p>}
    {(latestDraft.payload.unsupported_omissions??[]).length>0&&<p className="notice" role="status">
     这些原文被标成「已合并到别处」，但在正文里找不到对应内容（重合度低于 50%）。
     请把它们当作被丢弃的信息，而不是已写进简历：
     {(latestDraft.payload.unsupported_omissions??[]).slice(0,5).map(id=>{
      const segment=latestDraft.payload.segments?.find(entry=>entry.id===id);
      const audit=(latestDraft.payload.omission_audit??[]).find(entry=>entry.segment_id===id);
      const overlap=audit&&audit.overlap!==null?` · 重合度 ${Math.round((audit.overlap??0)*100)}%`:'';
      return <span className="d-coverage-missing" key={id}>{(segment?segment.text:`（段落 ${id}）`)+overlap}</span>;
     })}
    </p>}
   </div>}
   {(latestDraft.payload.segments??[]).length>0&&<details className="d-segments" data-testid="draft-segments">
    <summary>原文去向（{(latestDraft.payload.segments??[]).filter(segment=>!segment.heading).length} 段）</summary>
    <p className="fine-print">每段原文最终去了哪里：写进正文、成为固定段落、被明确省略，或没有说明。</p>
    <div className="d-segment-list">
     {(latestDraft.payload.segments??[]).filter(segment=>!segment.heading).map(segment=>{
      const section=idSection.get(segment.id),itemNo=idItem.get(segment.id),reason=idOmitted.get(segment.id);
      const where=section?`→ ${section}`:itemNo?`→ 正文第 ${itemNo} 条`:reason?`已省略（${reasonLabel[reason]??reason}）`:'未说明';
      const tone=section?'section':itemNo?'item':reason?'omitted':'missing';
      return <div className="d-segment" key={segment.id} data-where={tone}>
       <span className="tag">{segment.id}</span><span className={`d-segment-where d-where-${tone}`}>{where}</span>
       <span className="d-segment-text">{segment.text}</span></div>;
     })}
    </div>
   </details>}
   {rewriteItems.length>0?<>
    <p className="muted">下面四段由代码固定排版，内容取自你的原文。与原文不一致的地方可以直接改正；保存会生成新版本，旧版本不删除。</p>
    <div className="d-draft-sections">{sectionOrder.map(([key,label])=>
     <div className="d-draft-section" key={key} data-testid={`draft-section-${key}`}>
      <div className="d-draft-head"><span className="tag">{label}</span>
       <span className="fine-print">{sections[key].length===0?'原文里没有识别到这一段，不会自动补写':'取自原文，可逐行改正'}</span></div>
      {sections[key].map((line,index)=><div className="d-draft-line" key={index}>
       <textarea aria-label={`${label}第 ${index+1} 行`} value={line} disabled={busy||cancelled}
        onChange={e=>setSections(current=>({...current,[key]:current[key].map((value,i)=>i===index?e.target.value:value)}))}/>
       <button type="button" className="text-button" aria-label={`删除${label}第 ${index+1} 行`} disabled={busy||cancelled}
        onClick={()=>setSections(current=>({...current,[key]:current[key].filter((_,i)=>i!==index)}))}>删除</button>
      </div>)}
      <button type="button" className="text-button" data-testid={`draft-section-${key}-add`}
       disabled={busy||cancelled||sections[key].length>=8}
       onClick={()=>setSections(current=>({...current,[key]:[...current[key],'']}))}>添加一行</button>
     </div>)}
    </div>
    <p className="muted">叙述条目：逐条改成你自己的说法；保存同样生成新版本。</p>
    <div className="d-draft-items">{rewriteItems.map((item,index)=>{const prov=latestDraft?.provenance?.[index];return <div className="d-draft-item" key={index}>
     <div className="d-draft-head"><span className="tag">{index+1}</span><span className="fine-print">来源 {item.source_ids.length} 段 · {item.change_note}</span></div>
     {prov&&<p className="fine-print" data-testid={`draft-provenance-${index}`}
      data-transformation={prov.transformation} data-finding={prov.finding?.code??''}>
      来源：{prov.sources.map(source=>`${source.name??'未命名材料'}${source.content_sha256?` · ${source.content_sha256.slice(0,8)}`:''}`).join('、')||'未记录'}
      {' · '}{prov.transformation==='imported'?'按原文导入':prov.transformation==='user_edit'?'用户编辑':'模型改写'}
      {prov.segments.length>0&&` · 引用原文「${prov.segments[0].text.slice(0,40)}」`}
      {prov.finding&&` · 证据不支持（${prov.finding.code}），已移入建议`}
     </p>}
     <textarea aria-label={`草稿第 ${index+1} 条`} value={draftTexts[index]??''} disabled={busy||cancelled}
      onChange={e=>setDraftTexts(current=>current.map((value,i)=>i===index?e.target.value:value))}/>
    </div>})}</div>
    {draftSuggestions.length>0&&<fieldset className="d-suggestions" aria-label="可确认的加分项">
     <legend>可确认的加分项（不会自动写进简历）</legend>
     <p className="muted">这些是补充方向，不是已经发生的事实。<strong>勾选只记录“你已确认这些方向”</strong>，正文不会被自动改写；要写进简历，请在正文里自己补写后再保存。</p>
     {draftSuggestions.map((text,index)=><label className="d-suggestion" key={index}>
      <input type="checkbox" checked={pickedSuggestions.includes(index)} disabled={busy||cancelled}
       onChange={e=>setPickedSuggestions(current=>e.target.checked?[...current,index]:current.filter(i=>i!==index))}/>
      <span>{text}</span>
     </label>)}
     <p className="fine-print">正文只写来源支持的内容；勾选前请确认这条补充是否属实。</p>
    </fieldset>}
    <div className="d-form-foot"><span className="muted">修改只影响叙述，不改动已确认依据。</span>
     <button type="button" className="primary" disabled={busy||cancelled} onClick={()=>void saveDraft()}>保存为新版本</button></div>
   </>:<pre className="d-md">{latestDraft.markdown}</pre>}
  </section>}
 </div>;
}
