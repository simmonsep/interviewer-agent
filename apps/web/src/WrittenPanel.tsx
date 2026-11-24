import {useCallback,useEffect,useRef,useState} from 'react';
import {api,post,ApiError,describeError} from './api.ts';

interface Bank{bank_id:string;version:number;name:string;item_count:number;enabled:boolean;origin:string}
interface Question{question_id:string;kind:string;title:string;question:string;
 options:{option_id:string;text:string}[];bank_item:{name:string;version:number};answer:{option_ids:string[];text:string}|null}
interface GradedItem{question_id:string;status:string;reference:string;declared_correct_option_ids:string[];
 // F07Z-71: derived from the bank's reference on read; never a score, never stored back.
 reference_points?:string[]}
interface Report{items:GradedItem[];declared_key_question_count:number;declared_key_match_count:number;
 answer_key_verification:string;text_semantic_grading_performed:boolean;ability_certified:boolean}
interface Session{id:string;status:string;version:number;kind:string;deadline_at:string|null;questions:Question[];
 question_banks:{name:string;version:number}[];report:Report|null}
// 2026-11-13 用户决定：代码题不在产品范围内（按「没做过」处理）。后端 coding 模块与这里的
// 组件代码保留为历史资产、默认执行器关闭；界面不再出现代码题入口、进行中界面与结果。
const CODING_VISIBLE=false;

// A code session is the same endpoint's other shape: a task plus submissions, no questions.
interface CodeCase{id:string;visibility?:string;verdict:string;input?:string;output?:string}
interface CodeResult{verdict?:string;public_cases?:CodeCase[];cases?:CodeCase[];execution_error?:string|null;diagnostic?:string|null}
interface CodeSubmission{id:string;seq:number;status:string;error_code:string|null;source:string;result:CodeResult|null}
interface CodeTask{id:string;title:string;topic_id:string;statement:string;starter:string;entrypoint:string;
 language:string;version:string;origin:string;samples:CodeCase[]}
interface CodeReport{analysis:string;graded_submissions:number;pending_submissions:number;next_action:string;
 ability_certified:boolean;external_assistance_verified:boolean}
interface CodeSession{id:string;status:string;version:number;revision:number;kind:string;deadline_at:string|null;
 task:CodeTask;submissions:CodeSubmission[];report:CodeReport|null;policy:{image?:string;max_submissions?:number}}
interface Catalog{execution_enabled:boolean;items:CodeTask[]}
// The listing endpoint returns full session views, not a slim summary.
interface Summary{id:string;status:string;kind:string;question_banks?:{name:string;version:number}[];
 questions?:{question_id:string}[];task?:{title:string}}

const statusText:Record<string,string>={ACTIVE:'进行中',COMPLETED:'已结束',CANCELLED:'已取消'};
const gradeText:Record<string,string>={unanswered:'未作答',matches_declared_key:'与声明的答案一致',
 does_not_match_declared_key:'与声明的答案不一致',ungraded_missing_key:'选择题未提供答案键',
 requires_human_review:'简答，需人工看',recorded_without_reference:'已记录，无参考答案'};
const verdictText:Record<string,string>={ACCEPTED:'通过',WRONG_ANSWER:'输出不符',COMPILE_ERROR:'编译失败',
 ENVIRONMENT_ERROR:'执行环境错误',CANCELLED:'已取消'};

const isChoice=(question:Question)=>question.kind==='single_choice'||question.kind==='multiple_choice';

export function WrittenPanel({goalId,version,cancelled,onAuthLost,onGoalRefresh,openRequest}:{
 goalId:string;version:number;cancelled:boolean;onAuthLost:()=>void;onGoalRefresh:()=>void;
 /** F07Z-89: a session created elsewhere (the question bank's picker) to open here. */
 openRequest?:string|null;
}){
 const [banks,setBanks]=useState<Bank[]>([]),[sessions,setSessions]=useState<Summary[]>([]);
 const [session,setSession]=useState<Session|null>(null),[bankKey,setBankKey]=useState(''),[role,setRole]=useState<'java-backend'|'ai-application'>('java-backend');
 const [maxQuestions,setMaxQuestions]=useState(4),[drafts,setDrafts]=useState<Record<string,string>>({});
 const [choices,setChoices]=useState<Record<string,string[]>>({});
 const [catalog,setCatalog]=useState<Catalog|null>(null),[code,setCode]=useState<CodeSession|null>(null);
 const [source,setSource]=useState('');
 // Ticks are a private review aid: kept in memory only, never sent anywhere.
 const [pointTicks,setPointTicks]=useState<Record<string,boolean>>({});
 const [error,setError]=useState(''),[notice,setNotice]=useState(''),[busy,setBusy]=useState(false);
 const alive=useRef(true),lock=useRef(false);

 const load=useCallback(async()=>{
  try{
   const [bankView,list,cat]=await Promise.all([
    api<{banks:Bank[]}>('/api/question-banks'),
    api<{items:Summary[]}>(`/api/career-goals/${goalId}/written`),
    api<Catalog>(`/api/career-goals/${goalId}/written-catalog`),
   ]);
   if(!alive.current)return;
   const usable=bankView.banks.filter(bank=>bank.enabled&&bank.origin!=='builtin-programming');
   setBanks(usable);
   setSessions(list.items);
   setCatalog(cat);
   setBankKey(current=>current||(usable[0]?`${usable[0].bank_id}|${usable[0].version}`:''));
  }catch(e){if(alive.current){if(e instanceof ApiError&&e.status===401)onAuthLost();else setError(describeError(e));}}
 },[goalId,onAuthLost]);

 useEffect(()=>{alive.current=true;void load();return()=>{alive.current=false;};},[load]);

 function failure(e:unknown){
  if(e instanceof ApiError&&e.status===401){onAuthLost();return;}
  if(e instanceof ApiError&&e.code==='written_in_progress'){setError('已经有一场笔试没结束，先完成或取消它。');return;}
  if(e instanceof ApiError&&e.code==='question_bank_role_not_supported'){setError('这个题库里没有所选角色的题，换一个题库或换角色。');return;}
  if(e instanceof ApiError&&e.code==='version_conflict'){setError('目标或会话已更新，已重新读取。');void onGoalRefresh();return;}
  if(e instanceof ApiError&&e.code==='written_executor_disabled'){
   setError('本机未启用代码执行：启动时没有配置 --written-executor-profile，也没有可用的 Docker 执行镜像。代码题不会给出分数。');return;}
  if(e instanceof ApiError&&e.code==='written_execution_budget_exhausted'){setError('这场代码题的提交次数或目标的执行次数已用完。');return;}
  if(e instanceof ApiError&&e.code==='invalid_source'){setError('源码为空或超过 8KB 上限，未提交。');return;}
  if(e instanceof ApiError&&e.code==='java_programming_task_required'){setError('这个题库条目不是可执行的 Java 题。');return;}
  if(e instanceof ApiError&&e.status===400){setError('这道题的答案格式不对：选择题只选选项，简答题只填文字。');return;}
  setError(describeError(e));
 }
 async function run(action:()=>Promise<void>){
  if(lock.current)return;lock.current=true;setBusy(true);setError('');setNotice('');
  try{await action();}catch(e){failure(e);}finally{lock.current=false;if(alive.current)setBusy(false);}
 }

 const adopt=(value:Session)=>{
  setSession(value);
  setDrafts(Object.fromEntries(value.questions.map(question=>[question.question_id,question.answer?.text??''])));
  setChoices(Object.fromEntries(value.questions.map(question=>[question.question_id,question.answer?.option_ids??[]])));
  setNotice('');
 };

 const open=useCallback((id:string)=>run(async()=>{
  const value=await api<Session&Partial<CodeSession>>(`/api/career-goals/${goalId}/written/${id}`);
  if(!alive.current)return;
  if(value.kind==='question_set'){setCode(null);adopt(value);return;}
  setSession(null);setCode(value as CodeSession);setSource('');
 }),[goalId]);

 // Opening a session that another pane created. The request id is the guard: the same value is
 // opened once, and a later manual click on a history row is unaffected.
 const openedRequest=useRef<string|null>(null);
 useEffect(()=>{
  if(!openRequest||openedRequest.current===openRequest)return;
  openedRequest.current=openRequest;
  void open(openRequest).then(()=>{void load();});
 },[openRequest,open,load]);

 const start=()=>run(async()=>{
  const [bankId,bankVersion]=bankKey.split('|');
  if(!bankId)return;
  const value=await post<Session>(`/api/career-goals/${goalId}/written`,
   {expected_version:version,question_banks:[{bank_id:bankId,version:Number(bankVersion)}],role,max_questions:maxQuestions},crypto.randomUUID());
  if(!alive.current)return;
  setCode(null);adopt(value);setNotice('笔试已开始，按题作答；全部答完后点结束。');
  await load();
 });

 const startCode=(taskId:string)=>run(async()=>{
  const value=await post<Session&CodeSession>(`/api/career-goals/${goalId}/written`,
   {expected_version:version,task_id:taskId},crypto.randomUUID());
  if(!alive.current)return;
  setSession(null);setCode(value as CodeSession);
  setSource(value.task?.starter??'');
  setNotice('代码题已开始：改好 Main.java 后提交，最多三次。');
  await load();
 });

 const save=async(question:Question)=>{
  if(!session)return;
  await run(async()=>{
   const choice=isChoice(question);
   const body={expected_version:session.version,question_id:question.question_id,
    option_ids:choice?(choices[question.question_id]??[]):[],
    text:choice?'':(drafts[question.question_id]??'')};
   const value=await post<{session:Session}>(`/api/career-goals/${goalId}/written/${session.id}/answers`,body,crypto.randomUUID());
   if(alive.current){setSession(value.session);setNotice('这一题已保存。');}
  });
 };

 const finish=()=>run(async()=>{
  if(!session)return;
  const value=await post<Session>(`/api/career-goals/${goalId}/written/${session.id}/finish`,{expected_version:session.version},crypto.randomUUID());
  if(!alive.current)return;
  setSession(value);setNotice('已结束，下面是这次的可见结果。');
  await load();
 });

 const cancel=()=>run(async()=>{
  if(!session)return;
  const value=await post<Session>(`/api/career-goals/${goalId}/written/${session.id}/cancel`,{expected_version:session.version},crypto.randomUUID());
  if(!alive.current)return;
  setSession(value);await load();
 });

 const submitCode=()=>run(async()=>{
  if(!code)return;
  const value=await post<{submission_id:string;session:CodeSession}>(`/api/career-goals/${goalId}/written/${code.id}/submissions`,
   {expected_version:code.version,source},crypto.randomUUID());
  if(!alive.current)return;
  setCode(value.session);setNotice('提交已受理：执行结果需要刷新查看，或者等它跑完再刷新。');
  await load();
 });

 const refreshCode=()=>run(async()=>{
  if(!code)return;
  const value=await api<CodeSession>(`/api/career-goals/${goalId}/written/${code.id}`);
  if(!alive.current)return;
  setCode(value);
 });

 const finishCode=()=>run(async()=>{
  if(!code)return;
  const value=await post<CodeSession>(`/api/career-goals/${goalId}/written/${code.id}/finish`,{expected_version:code.version},crypto.randomUUID());
  if(!alive.current)return;
  setCode(value);setNotice('已结束，下面是这道题的可见结果。');
  await load();
 });

 const active=session?.status==='ACTIVE';
 const bank=session?.question_banks[0];
 const codeActive=code?.status==='ACTIVE';
 const maxSubmissions=code?.policy?.max_submissions??3;

 return <div className="d-written">
  {error&&<p role="alert" className="error">{error}</p>}
  {notice&&<p className="notice" role="status">{notice}</p>}
  {cancelled&&<p className="notice">当前目标已结束，笔试只读。</p>}

  {!session&&!code&&<section className="card d-card" aria-label="开始笔试">
   <div className="d-card-head"><h2>笔试 · 文字题</h2></div>
   <p className="muted">从题库抽一套题，逐题作答后结束。选择题选选项，简答题写文字。</p>
   {banks.length===0?<p className="fine-print">没有可用题库。先去「题库」导入或启用一个。</p>:<>
    <div className="career-fields">
     <label>题库<select aria-label="笔试题库" value={bankKey} onChange={e=>setBankKey(e.target.value)}>
      {banks.map(item=><option key={`${item.bank_id}|${item.version}`} value={`${item.bank_id}|${item.version}`}>{item.name} · v{item.version} · {item.item_count} 题</option>)}
     </select></label>
     <label>角色<select aria-label="笔试角色" value={role} onChange={e=>setRole(e.target.value as typeof role)}>
      <option value="java-backend">Java 后端</option><option value="ai-application">AI 应用</option>
     </select></label>
     <label>题数<input aria-label="笔试题数" type="number" min={1} max={20} value={maxQuestions} onChange={e=>setMaxQuestions(Number(e.target.value))}/></label>
    </div>
    <div className="d-form-foot"><span className="fine-print">答案键由题库作者声明，系统不做独立核验。</span>
     <button type="button" className="primary" disabled={busy||cancelled||!bankKey} onClick={()=>void start()}>开始笔试</button></div>
   </>}
  </section>}

  {CODING_VISIBLE&&!session&&!code&&<section className="card d-card" aria-label="开始代码题">
   <div className="d-card-head"><h2>笔试 · 代码题</h2>
    {catalog&&<span className={`tag ${catalog.execution_enabled?'done':'bad'}`}>{catalog.execution_enabled?'本机可执行':'未启用执行'}</span>}</div>
   {!catalog?<p className="fine-print">正在读取题面…</p>:<>
    {!catalog.execution_enabled&&<p className="notice" role="status">本机未启用代码执行：启动时没有配置 <code>--written-executor-profile</code>，也没有可用的 Docker 执行镜像。可以看题面，但不会创建会话，也不会给出分数。</p>}
    <p className="muted">题目是项目自带的合成题，不是公司真题；执行在本机 Docker 沙箱里进行，判分只比对声明的用例。</p>
    <div className="d-code-list">{catalog.items.map(item=><article key={item.id} className="d-code-item">
     <div className="d-written-head"><strong>{item.title}</strong><span className="tag d-num d-num-sm">{item.language}</span></div>
     <p className="d-written-question">{item.statement}</p>
     {item.samples.length>0&&<details className="d-code-sample"><summary>公开样例（{item.samples.length}）</summary>
      {item.samples.map(sample=><div key={sample.id} className="d-code-io">
       <span className="fine-print">输入</span><pre>{sample.input}</pre>
       <span className="fine-print">输出</span><pre>{sample.output}</pre></div>)}
     </details>}
     <div className="d-form-foot"><span className="fine-print">{item.entrypoint}</span>
      <button type="button" className="secondary" disabled={busy||cancelled||!catalog.execution_enabled}
       onClick={()=>void startCode(item.id)}>开始代码题</button></div>
    </article>)}</div>
   </>}
  </section>}

  {session&&<section className="card d-card" aria-label="笔试题目">
   <div className="d-card-head"><h2>{active?'作答中':'本次笔试'}</h2>
    <span className={`tag ${session.status==='COMPLETED'?'done':'bad'}`}>{statusText[session.status]??session.status}</span></div>
   <p className="muted">{bank?`${bank.name} · v${bank.version}`:''} · 共 {session.questions.length} 题 · 会话 {session.id.slice(0,8)}</p>
   <div className="d-written-list">{session.questions.map((question,index)=>{
    const graded=session.report?.items.find(item=>item.question_id===question.question_id);
    const pick=choices[question.question_id]??[];
    return <article className="d-written-item" key={question.question_id}>
     <div className="d-written-head"><span className="tag d-num d-num-sm">{index+1}</span><strong>{question.title}</strong>
      {graded&&<span className={`tag ${graded.status==='matches_declared_key'?'done':graded.status==='does_not_match_declared_key'?'bad':''}`}>{gradeText[graded.status]??graded.status}</span>}</div>
     <p className="d-written-question">{question.question}</p>
     {active?<>
      {isChoice(question)?<fieldset className="d-written-options" aria-label={`第 ${index+1} 题选项`}>
       {question.options.map(option=>{
        const single=question.kind==='single_choice';
        return <label key={option.option_id} className="d-written-option">
         <input type={single?'radio':'checkbox'} name={`q-${question.question_id}`}
          checked={pick.includes(option.option_id)} disabled={busy}
          onChange={()=>setChoices(current=>{
           const now=current[question.question_id]??[];
           const next=single?[option.option_id]
            :now.includes(option.option_id)?now.filter(id=>id!==option.option_id):[...now,option.option_id];
           return {...current,[question.question_id]:next};
          })}/>
         <span>{option.text}</span></label>;
       })}
      </fieldset>:<textarea aria-label={`第 ${index+1} 题作答`} value={drafts[question.question_id]??''} maxLength={1500} disabled={busy}
       placeholder="用自己的话写，不必写成标准答案。" onChange={e=>setDrafts(current=>({...current,[question.question_id]:e.target.value}))}/>}
      <div className="d-written-foot"><span className="fine-print d-num d-num-sm">
       {isChoice(question)?`已选 ${pick.length} 项`:`${(drafts[question.question_id]??'').length} / 1500`}</span>
       <button type="button" className="secondary" disabled={busy} onClick={()=>void save(question)}>保存这一题</button></div>
     </>:<>
      {isChoice(question)&&question.options.length>0&&<ul className="d-written-answer">
       {question.options.map(option=><li key={option.option_id}>
        {pick.includes(option.option_id)?'✓':'·'} {option.text}</li>)}
      </ul>}
      {question.answer?.text&&<blockquote className="d-written-answer">{question.answer.text}</blockquote>}
      {graded?.reference&&<p className="fine-print">参考答案：{graded.reference}</p>}
      {!isChoice(question)&&(graded?.reference_points?.length??0)>0&&
       <div className="d-written-points" data-testid="written-points">
        <p className="fine-print">参考要点（由题库参考答案按标点切出，不是评分标准；勾选只用于你自己复盘，不会保存或上传）</p>
        <ul>{graded!.reference_points!.map((point,pointIndex)=>{
         const key=`${question.question_id}:${pointIndex}`;
         const ticked=pointTicks[key]??false;
         return <li key={key}><label>
          <input type="checkbox" checked={ticked} data-testid={`written-point-${pointIndex}`}
           onChange={()=>setPointTicks(current=>({...current,[key]:!ticked}))}/>
          <span>{point}</span></label></li>;
        })}</ul>
       </div>}
     </>}
    </article>;
   })}</div>
   {active&&<div className="d-form-foot"><span className="fine-print">结束之后不能再改答案。</span>
    <div className="d-written-actions">
     <button type="button" className="text-button" disabled={busy} onClick={()=>void cancel()}>放弃本次</button>
     <button type="button" className="primary" disabled={busy} onClick={()=>void finish()}>结束并看结果</button></div></div>}
  </section>}

  {session?.report&&<section className="card d-card" aria-label="笔试结果">
   <div className="d-card-head"><h2>可见结果</h2></div>
   <div className="d-side-stats">
    <div><b className="d-num d-num-mid">{session.report.declared_key_match_count}</b><span>与答案键一致</span></div>
    <div><b className="d-num d-num-mid">{session.report.declared_key_question_count}</b><span>有答案键的题</span></div>
    <div><b className="d-num d-num-mid">{session.report.items.length}</b><span>总题数</span></div>
   </div>
   <p className="fine-print">答案键由题库作者声明，系统未做独立核验；简答题不做语义评分，只记录原文。下面的「参考要点」是从参考答案切出的清单，供你自己对照勾选，系统不判定你是否答到，也不代表能力认证。</p>
  </section>}

  {CODING_VISIBLE&&code&&<section className="card d-card" aria-label="代码题">
   <div className="d-card-head"><h2>{code.task.title}</h2>
    <span className={`tag ${code.status==='COMPLETED'?'done':'bad'}`}>{statusText[code.status]??code.status}</span></div>
   <p className="muted">会话 {code.id.slice(0,8)} · 最多 {maxSubmissions} 次提交 · {code.task.entrypoint}</p>
   <p className="d-written-question">{code.task.statement}</p>
   {code.task.samples.length>0&&<details className="d-code-sample"><summary>公开样例（{code.task.samples.length}）</summary>
    {code.task.samples.map(sample=><div key={sample.id} className="d-code-io">
     <span className="fine-print">输入</span><pre>{sample.input}</pre>
     <span className="fine-print">输出</span><pre>{sample.output}</pre></div>)}
   </details>}
   {codeActive?<>
    <label className="d-code-editor">Main.java
     <textarea aria-label="Main.java 源码" value={source} spellCheck={false} disabled={busy}
      onChange={e=>setSource(e.target.value)}/></label>
    <div className="d-form-foot"><span className="fine-print">源码只在本机执行；判分只比对声明的用例。</span>
     <div className="d-written-actions">
      <button type="button" className="text-button" disabled={busy} onClick={()=>void refreshCode()}>刷新结果</button>
      <button type="button" className="secondary" disabled={busy||!source.trim()} onClick={()=>void submitCode()}>提交源码</button>
      <button type="button" className="primary" disabled={busy} onClick={()=>void finishCode()}>结束并看结果</button></div></div>
   </>:<div className="d-form-foot"><span className="fine-print">这场已经结束，不能再提交。</span>
    <button type="button" className="text-button" disabled={busy} onClick={()=>{setCode(null);setSource('');}}>返回笔试首页</button></div>}
   {code.submissions.length>0&&<div className="d-code-submissions">
    {code.submissions.map(item=>{
     const cases=item.result?.cases??item.result?.public_cases??[];
     const verdict=item.result?.verdict??(item.status==='PENDING'?'PENDING':item.status==='RUNNING'?'RUNNING':item.status);
     return <article key={item.id} className="d-code-submission">
      <div className="d-written-head"><span className="tag d-num d-num-sm">#{item.seq}</span>
       <strong>{verdictText[verdict]??verdict}</strong>
       <span className="fine-print">{item.status==='SUCCEEDED'?'已执行':item.error_code??item.status}</span></div>
      {item.result?.execution_error&&<p className="error">{verdictText[item.result.execution_error]??item.result.execution_error}</p>}
      {item.result?.diagnostic&&<pre className="d-code-io-pre">{item.result.diagnostic}</pre>}
      {cases.length>0&&<div className="d-code-cases">{cases.map(c=><div key={c.id} className="d-code-case">
       <span className={`tag ${c.verdict==='ACCEPTED'?'done':'bad'}`}>{verdictText[c.verdict]??c.verdict}</span>
       <span className="fine-print">{c.id}{c.visibility?` · ${c.visibility}`:''}</span>
      </div>)}</div>}
     </article>;
    })}
   </div>}
  </section>}

  {CODING_VISIBLE&&code?.report&&<section className="card d-card" aria-label="代码题结果">
   <div className="d-card-head"><h2>可见结果</h2></div>
   <div className="d-side-stats">
    <div><b className="d-num d-num-mid">{code.report.graded_submissions}</b><span>已执行提交</span></div>
    <div><b className="d-num d-num-mid">{code.report.pending_submissions}</b><span>执行中</span></div>
    <div><b className="d-num d-num-mid">{code.submissions.length}</b><span>总提交</span></div>
   </div>
   <p className="write-pre">{code.report.analysis}</p>
   <p className="fine-print">{code.report.next_action} 未做能力认证，也不核验外部协助。</p>
  </section>}

  <section className="card d-card" aria-label="笔试历史">
   <div className="d-card-head"><h2>历史</h2><span className="n d-num d-num-sm">{sessions.filter(item=>item.kind!=='code').length}</span></div>
   {sessions.filter(item=>item.kind!=='code').length===0?<p className="fine-print">还没有笔试记录。</p>:
    <div className="d-session-list">{sessions.filter(item=>item.kind!=='code').map(item=><button key={item.id} type="button" className="d-session-item"
     onClick={()=>void open(item.id)}>
     <span><strong>{item.kind==='question_set'?`笔试 ${item.id.slice(0,8)}`:`代码题 ${item.id.slice(0,8)}`}</strong>
      <small>{item.kind==='question_set'
       ?(item.question_banks?.[0]?`${item.question_banks[0].name} · v${item.question_banks[0].version}`:'—')
       :(item.task?.title??'代码题')} · {item.kind==='question_set'?`${item.questions?.length??0} 题`:''}</small></span>
     <span className={`tag ${item.status==='COMPLETED'?'done':''}`}>{statusText[item.status]??item.status}</span>
    </button>)}</div>}
  </section>
 </div>;
}
