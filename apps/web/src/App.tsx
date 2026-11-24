import {useEffect,useRef,useState,type FormEvent} from 'react';
import type {PracticeQuestion,PracticeSessionView,PracticeFeedback} from './contracts.ts';
import {api,post,describeError,ApiError} from './api.ts';
import {CareerPanel} from './CareerPanel.tsx';
import {resolveShell,resolveTheme,storeTheme,systemPrefersDark,paintTheme,type Theme} from './shell.ts';
import {WindowStrip} from './WindowStrip.tsx';

interface User {id:string;name:string;email:string}
type HistoryItem=Pick<PracticeSessionView,'id'|'status'|'question'|'created_at'>;
const topics:Record<string,string>={'tool-use':'工具与权限',planning:'任务规划',context:'上下文管理',memory:'记忆与恢复',evaluation:'评测方法',rag:'检索与证据',reliability:'可靠性',backend:'后端基础','agent-design':'Agent设计'};
const phase:Record<string,string>={WAITING_FOR_ANSWER:'等待作答',EVALUATING:'分析回答中',COMPLETED:'反馈已保存',CANCELLED:'已取消',FAILED:'本次反馈未完成'};
const waitMessage:Record<string,string>={model_not_enabled:'后台评价器尚未启用。启用本机脚本评价器后，可以继续本次流程演示。',model_credentials_unavailable:'本机尚未配置本次模型凭证，请完成配置后再继续。',model_authorization_unavailable:'本次模型调用授权尚未就绪，请先检查本机授权配置。',model_budget_exhausted:'本次调用额度已不足，已停止新请求；重复点击不会增加额度。',model_usage_unknown:'上次请求的费用尚不明确，已暂停新请求。请先核对本机费用记录。'};
const activeOperation=(s:PracticeSessionView)=>!!s.operation&&['PENDING','RUNNING'].includes(s.operation.status);
function draftKey(userId:string,id:string){return `py-practice:${userId}:${id}`;}

function AuthPanel({onLogin,variant='web'}:{onLogin:(user:User)=>void;variant?:'web'|'desktop'}){
 const [signup,setSignup]=useState(false),[error,setError]=useState(''),[busy,setBusy]=useState(false);const lock=useRef(false);
 async function submit(event:FormEvent<HTMLFormElement>){
  event.preventDefault();if(lock.current)return;lock.current=true;setBusy(true);setError('');
  const data=new FormData(event.currentTarget);
  try{const result=await post<{user:User}>(`/api/auth/${signup?'sign-up':'sign-in'}/email`,{email:data.get('email'),password:data.get('password'),...(signup?{name:data.get('name')}:{})});onLogin(result.user);}
  catch(e){setError(e instanceof ApiError&&e.status===401?'邮箱或密码不正确。':e instanceof ApiError&&e.status===422?'这个邮箱已注册，请切换到登录。':describeError(e));}
  finally{lock.current=false;setBusy(false);}
 }
 const form=<form onSubmit={submit}>{signup&&<label>称呼<input name="name" autoComplete="name" required maxLength={60} placeholder="怎么称呼你"/></label>}<label>邮箱<input name="email" type="email" autoComplete="email" required placeholder="you@example.com"/></label><label>密码<input name="password" type="password" autoComplete={signup?'new-password':'current-password'} required minLength={12} placeholder="至少12位"/></label>{error&&<p className="error" role="alert">{error}</p>}<button className="primary wide" disabled={busy}>{busy?'正在处理…':signup?'创建账号':'登录'}</button></form>;
 const switchButton=<button className="text-button" onClick={()=>{setSignup(!signup);setError('');}} disabled={busy}>{signup?'已有账号？去登录':'第一次来？创建账号'}</button>;
 if(variant==='desktop'){
  return <section className="desktop-auth">
   <div className="desktop-auth-intro"><span className="eyebrow">INTERVIEW WORKBENCH</span>
    <h1>把求职目标和面试训练，<br/>放在同一个桌面工作台。</h1>
    <p>登录后从当前目标继续：等你看题的场次、需要你作答的一题，和已完成的复盘，都在同一处。</p>
    <ol className="desktop-auth-steps"><li>打开工作台，确认当前目标</li><li>继续或开始一场两题面试</li><li>在复盘里按题查看依据</li></ol>
   </div>
   <div className="card desktop-auth-card"><span className="eyebrow">YOUR PRACTICE SPACE</span><h2>{signup?'建立你的练习空间':'欢迎回来'}</h2>
    <p className="muted">{signup?'保存回答和反馈，随时回来看。':'登录后继续已有练习，或开始新的一题。'}</p>
    {form}{switchButton}<p className="fine-print">当前为本机开发预览，请使用测试账号。</p></div>
  </section>;
 }
 return <section className="auth-layout"><div className="intro"><span className="eyebrow">PRACTICE, WITH EVIDENCE</span><h1>一题一练，<br/>把回答讲清楚。</h1><p>从岗位基础出发，用自己的话回答，<br/>再结合原文与参考资料，找到下一步。</p><div className="intro-notes"><span>01 选一道题</span><span>02 独立作答</span><span>03 查看反馈</span></div><p className="muted">AI 面试训练 · 无需上传简历或职位说明</p></div><div className="card auth-card"><span className="eyebrow">YOUR PRACTICE SPACE</span><h2>{signup?'建立你的练习空间':'欢迎回来'}</h2><p className="muted">{signup?'保存回答和反馈，随时回来看。':'登录后继续已有练习，或开始新的一题。'}</p>{form}{switchButton}<p className="fine-print">当前为本机开发预览，请使用测试账号。</p></div></section>;
}
function FeedbackPanel({feedback}:{feedback:PracticeFeedback}){
 return <section className="feedback" aria-label="训练反馈"><div className="section-heading"><h2>回看这一题</h2><span className="tag">不计正式分数</span></div><h3>{feedback.summary}</h3><p className="feedback-text">{feedback.feedback}</p><p className="fine-print">{feedback.origin==='real_model'?'本次反馈由模型生成，尚未经过人工复核。':'本次为确定性脚本流程演示，不代表模型评价效果。'}</p><h3>反馈依据</h3><div className="citations">{feedback.citations.map((c,index)=><blockquote key={`${c.source_id}-${index}`}><span className="citation-label">{c.kind==='answer'?'你的回答原文':'参考资料摘要'} · 第{c.start_line}{c.end_line!==c.start_line?`–${c.end_line}`:''}行</span><p>{c.quote}</p>{c.source&&<a href={c.source.url} target="_blank" rel="noopener noreferrer">{c.source.title} ↗</a>}</blockquote>)}</div><p className="fine-print">参考内容为依据公开资料整理的摘要。反馈用于练习，尚未经独立人工审核。</p></section>;
}
function TrainingRoom({user,session,onRefresh,onNew,onAuthLost}:{user:User;session:PracticeSessionView;onRefresh:()=>Promise<void>;onNew:()=>void;onAuthLost:()=>void}){
 const key=draftKey(user.id,session.id);
 const [text,setText]=useState(()=>sessionStorage.getItem(key)??''),[error,setError]=useState(''),[submitting,setSubmitting]=useState(false),[actionBusy,setActionBusy]=useState(false),[connection,setConnection]=useState('等待连接');
 const submittingRef=useRef(false),pendingKey=key+':submission',eventKey=key+':event';
 useEffect(()=>{if(session.answer)sessionStorage.removeItem(key);},[session.answer,key]);
 useEffect(()=>{
  if(!activeOperation(session))return;
  const operationId=session.operation!.id;let alive=true,refreshing=false;
  const refresh=async()=>{if(refreshing||!alive)return;refreshing=true;try{await onRefresh();}catch(e){if(alive&&e instanceof ApiError&&e.status===401)onAuthLost();}finally{refreshing=false;}};
  const cursor=sessionStorage.getItem(eventKey)??'0';
  const source=new EventSource(`/api/operations/${operationId}/events?after=${encodeURIComponent(cursor)}`);
  source.onopen=()=>{if(alive)setConnection('实时更新中');};
  source.addEventListener('stream-end',()=>{source.close();void refresh();});
  source.addEventListener('auth-expired',()=>{source.close();if(alive)onAuthLost();});
  source.addEventListener('unavailable',()=>{source.close();void refresh();});
  source.addEventListener('stream-error',()=>{source.close();if(alive)setConnection('实时连接暂不可用，正在定时同步');});
  source.addEventListener('operation',event=>{if(!alive)return;const message=event as MessageEvent;try{const data=JSON.parse(message.data);if(data.operation_id!==operationId)return;if(message.lastEventId)sessionStorage.setItem(eventKey,message.lastEventId);void refresh();}catch{setConnection('正在同步状态');}});
  source.onerror=()=>{if(alive){setConnection('连接恢复中，仍会同步结果');void refresh();}};
  const fallback=setInterval(()=>void refresh(),2500);
  return ()=>{alive=false;source.close();clearInterval(fallback);};
 },[session.id,session.operation?.id,session.operation?.status]);
 async function submit(event:FormEvent){
  event.preventDefault();if(submittingRef.current)return;submittingRef.current=true;setSubmitting(true);setError('');
  const body={expected_version:session.version,text};
  let previous:{key:string;body:typeof body}|null=null;try{previous=JSON.parse(sessionStorage.getItem(pendingKey)??'null');}catch{}
  const entry=previous&&JSON.stringify(previous.body)===JSON.stringify(body)?previous:{key:crypto.randomUUID(),body};sessionStorage.setItem(pendingKey,JSON.stringify(entry));
  try{await post(`/api/practice-sessions/${session.id}/answers`,entry.body,entry.key);await onRefresh();}
  catch(e){setError(describeError(e));if(e instanceof ApiError&&e.status===401)onAuthLost();try{await onRefresh();}catch{}}
  finally{submittingRef.current=false;setSubmitting(false);}
 }
 async function action(kind:'cancel'|'resume'){
  if(!session.operation||actionBusy)return;setActionBusy(true);setError('');
  try{await post(`/api/operations/${session.operation.id}/${kind}`,{});await onRefresh();}catch(e){setError(describeError(e));if(e instanceof ApiError&&e.status===401)onAuthLost();}finally{setActionBusy(false);}
 }
 const waiting=session.operation?.status==='WAITING_CREDENTIALS',working=activeOperation(session);
 return <div className="room"><section className="card question-card"><div className="section-heading"><span className="eyebrow">ONE QUESTION · 单题讲解训练</span><button className="text-button" onClick={onNew}>返回练习首页</button></div><div className="chips"><span className="tag">AI 应用 / Agent 实习</span><span className="tag">{topics[session.question.topic]??session.question.topic}</span><span className="phase">{phase[session.status]??session.status}</span></div><h1 className="question-title">{session.question.text}</h1><p className="muted">先用自己的话说明判断过程；不必套用固定措辞。</p>{session.status==='WAITING_FOR_ANSWER'?<form onSubmit={submit}><label htmlFor="answer">你的回答</label><textarea id="answer" value={text} onChange={e=>{setText(e.target.value);sessionStorage.setItem(key,e.target.value);}} maxLength={2000} rows={9} placeholder="写下你的判断、理由，以及会怎样处理…" disabled={submitting}/><div className="answer-toolbar"><span className="muted">{text.length}/2000 · 最多40行</span><button className="primary" disabled={submitting||!text.trim()||text.split(/\r?\n/).length>40}>{submitting?'正在保存回答…':'提交并查看反馈 →'}</button></div></form>:session.answer&&<div className="saved-answer"><span className="eyebrow">你的回答 · 已保存</span><p>{session.answer.text}</p></div>}{error&&<p className="error" role="alert">{error}</p>}</section>{working&&<section className="card status-card" role="status"><span className="pulse"/><div><h2>{session.operation?.status==='PENDING'?'回答已保存，正在等待分析':'正在结合你的回答与参考资料分析'}</h2><p className="muted">{connection}。可以离开页面，返回后继续查看。</p></div><button className="secondary" disabled={actionBusy} onClick={()=>void action('cancel')}>取消分析</button></section>}{waiting&&<section className="card status-card"><div><h2>回答已保存，等待模型可用</h2><p className="muted">{waitMessage[session.operation?.error_code??'']??'当前评价暂不可用，原回答已保留。'}</p></div><button className="secondary" onClick={()=>void action('resume')} disabled={actionBusy||['model_budget_exhausted','model_usage_unknown'].includes(session.operation?.error_code??'')}>重试获取反馈</button><button className="text-button" onClick={()=>void action('cancel')} disabled={actionBusy}>取消</button></section>}{session.feedback&&<div className="card"><FeedbackPanel feedback={session.feedback}/></div>}{['FAILED','CANCELLED'].includes(session.status)&&<section className="card"><h2>{session.status==='CANCELLED'?'已取消分析，原回答仍保留':'本次反馈未完成，原回答仍保留'}</h2><p className="muted">可以返回首页重新练习。页面不会把失败结果当作已完成反馈。</p><button className="secondary" onClick={onNew}>开始新练习</button></section>}</div>;
}
 // 本机身份：桌面窗口打开即用；它是本机服务里的一个真实账号，因此历史照常积累，
 // 但不需要任何登录动作。切换到其他账号仍然可用（设置 → 账号）。
 //
// F07Z-83: in the desktop app the password no longer lives in localStorage - that is a plain
// file in the webview profile, readable by anything that can read the profile. It goes to the
// Windows Credential Manager through the host's three secret commands, and the page keeps only
// the address and the display name. A browser tab has no host to hold a secret, so development
// keeps the old shape there; the packaged app never takes that branch.
const IDENTITY_KEY='py-local-identity';
type StoredIdentity={email?:string;name?:string;password?:string};
function readStoredIdentity():StoredIdentity|null{
 try{const raw=localStorage.getItem(IDENTITY_KEY);return raw?(JSON.parse(raw) as StoredIdentity):null;}catch{return null;}
}
function saveIdentity(value:{email:string;name:string}){
 try{localStorage.setItem(IDENTITY_KEY,JSON.stringify(value));}catch{/* storage may be blocked; this session still works */}
}
type HostCore={invoke<T>(command:string,args?:Record<string,unknown>):Promise<T>};
function hostCore():HostCore|undefined{
 return (window as unknown as {__TAURI__?:{core?:HostCore}}).__TAURI__?.core;
}
// The local identity's address carries its id, which is also the key of its credential slot:
// `local-<uuid>@desktop.example.com`.
function identityId(email:string|undefined):string|null{
 const found=/^local-([0-9a-f-]{36})@desktop\.example\.com$/i.exec(email??'');
 return found?found[1].toLowerCase():null;
}
async function readSecret(core:HostCore,id:string):Promise<string|null>{
 try{return await core.invoke<string|null>('local_identity_secret_get',{id});}catch{return null;}
}
async function writeSecret(core:HostCore,id:string,secret:string):Promise<boolean>{
 try{await core.invoke('local_identity_secret_set',{id,secret});return true;}catch{return false;}
}
/** Moves a password that an older build left in localStorage into the OS store. Runs once per
 *  window start, whether or not a session already exists - the silent sign-in path returns early
 *  for a signed-in user, and that is exactly the case that would otherwise keep the secret. */
async function migrateIdentitySecret():Promise<void>{
 const stored=readStoredIdentity();
 const core=hostCore();
 const id=identityId(stored?.email);
 if(!core||!stored?.email||!stored.password||!id)return;
 if(await writeSecret(core,id,stored.password))
  saveIdentity({email:stored.email,name:stored.name??'本机用户'});
}
async function localIdentity():Promise<{email:string;password:string;name:string}|null>{
 const stored=readStoredIdentity();
 const core=hostCore();
 if(!core){
  if(stored?.email&&stored.password)return {email:stored.email,password:stored.password,name:stored.name??'本机用户'};
  const created={email:`local-${crypto.randomUUID()}@desktop.example.com`,password:`Local-${crypto.randomUUID()}-key`,name:'本机用户'};
  try{localStorage.setItem(IDENTITY_KEY,JSON.stringify(created));}catch{return null;}
  return created;
 }
 // A password still sitting in the stored identity belongs to that identity, so it moves to the
 // OS store first.
 const storedId=identityId(stored?.email);
 if(stored?.email&&stored.password&&storedId){
  if(!await writeSecret(core,storedId,stored.password))return null;
  saveIdentity({email:stored.email,name:stored.name??'本机用户'});
  return {email:stored.email,password:stored.password,name:stored.name??'本机用户'};
 }
 if(stored?.email&&storedId){
  const secret=await readSecret(core,storedId);
  if(!secret)return null; // known identity, no slot for it: fall back to the login screen
  return {email:stored.email,password:secret,name:stored.name??'本机用户'};
 }
 const created={email:`local-${crypto.randomUUID()}@desktop.example.com`,password:`Local-${crypto.randomUUID()}-key`,name:'本机用户'};
 const createdId=identityId(created.email);
 if(!createdId||!await writeSecret(core,createdId,created.password))return null;
 saveIdentity({email:created.email,name:created.name});
 return created;
}
export function App(){
 const shell=useState(resolveShell)[0];
 // The browser default is the workbench on this branch, so the tab title must say so.
 useEffect(()=>{document.title=shell==='desktop'?'求职工作台':'练习间 · 面试训练';},[shell]);
 // Appearance is resolved once, before the first desktop paint, so the window never
 // flashes the wrong theme; `auto` follows the OS and re-renders when it changes.
 const [theme,setTheme]=useState<Theme>(resolveTheme);
 const [systemDark,setSystemDark]=useState(systemPrefersDark);
 const activeTheme=paintTheme(theme,systemDark);
 useEffect(()=>{
  let media:MediaQueryList;
  try{media=window.matchMedia('(prefers-color-scheme: dark)');}catch{return;}
  const onChange=(event:MediaQueryListEvent)=>setSystemDark(event.matches);
  media.addEventListener('change',onChange);
  return ()=>media.removeEventListener('change',onChange);
 },[]);
 const changeTheme=(next:Theme)=>{storeTheme(next);setTheme(next);};
 const [workspace,setWorkspace]=useState<'practice'|'career'>(()=>new URLSearchParams(window.location.search).has('goal')?'career':'practice');
 const [assessmentMode,setAssessmentMode]=useState<'scripted'|'live'>('scripted');
 const [user,setUser]=useState<User|null>(null),[loading,setLoading]=useState(true),[questions,setQuestions]=useState<PracticeQuestion[]>([]),[questionId,setQuestionId]=useState(''),[history,setHistory]=useState<HistoryItem[]>([]),[session,setSession]=useState<PracticeSessionView|null>(null),[error,setError]=useState(''),[starting,setStarting]=useState(false);
 // 桌面直入：false 时用本机身份静默进入；true 表示用户主动要看登录页（切换账号/退出后）。
 // `py-desktop-login` 是一次性标志（读取后即清除）：验收与深链可以要求这一屏是登录页，
 // 之后的刷新回到常规免登流程。
 const [loginRequested,setLoginRequested]=useState(()=>{try{return localStorage.getItem('py-desktop-login')==='1';}catch{return false;}});
 useEffect(()=>{try{if(localStorage.getItem('py-desktop-login')==='1')localStorage.removeItem('py-desktop-login');}catch{}},[]);
 useEffect(()=>{void migrateIdentitySecret();},[]);
 const refreshSequence=useRef(0);
 const epoch=useRef(0),startLock=useRef(false),startRequest=useRef<{questionId:string;targetKey:string;sessionKey:string}|null>(null),sessionRef=useRef<PracticeSessionView|null>(null);sessionRef.current=session;
 const setActive=(value:PracticeSessionView|null)=>{sessionRef.current=value;setSession(value);};
 function authLost(){epoch.current++;startRequest.current=null;setActive(null);setUser(null);setHistory([]);setQuestions([]);setError('登录已过期，请重新登录。');}
 async function loadHistory(signal?:AbortSignal){const current=epoch.current;const result=await api<{items:HistoryItem[]}>('/api/practice-sessions',{signal});if(current===epoch.current&&!signal?.aborted)setHistory(result.items);}
 async function open(id:string){const current=++epoch.current;setError('');setActive(null);try{const value=await api<PracticeSessionView>(`/api/practice-sessions/${id}`);if(current!==epoch.current)return;setActive(value);window.history.replaceState(null,'',`/?session=${value.id}`);}catch(e){if(current===epoch.current){setError(describeError(e));if(e instanceof ApiError&&e.status===401)authLost();}}}
 async function refresh(){
  const before=sessionRef.current;if(!before)return;
  const current=epoch.current,sequence=++refreshSequence.current;
  const isCurrent=()=>current===epoch.current&&sequence===refreshSequence.current&&sessionRef.current?.id===before.id;
  try{const value=await api<PracticeSessionView>(`/api/practice-sessions/${before.id}`);
   if(isCurrent()&&value.version>=sessionRef.current!.version){setActive(value);if(['COMPLETED','FAILED','CANCELLED'].includes(value.status))void loadHistory().catch(()=>{});}
  }catch(e){if(isCurrent())throw e;}
 }
 useEffect(()=>{const controller=new AbortController();void api<{user:User}|null>('/api/auth/get-session',{signal:controller.signal}).then(result=>{if(!controller.signal.aborted)setUser(result?.user??null);}).catch(()=>{}).finally(()=>{if(!controller.signal.aborted)setLoading(false);});return()=>controller.abort();},[]);
 // Desktop direct entry: silently sign in with (or create) the local identity, so the
 // workbench is the first thing the user sees. The login page appears only when the
 // user asks for it (switch account) or the silent path cannot complete.
 useEffect(()=>{
  if(shell!=='desktop'||loading||user||loginRequested)return;
  let alive=true;
  void (async()=>{
   // The secret comes from the host, so the identity is resolved before signing in.
   const identity=await localIdentity();
   if(!alive)return;
   if(!identity){setLoginRequested(true);return;}
   try{
    await post('/api/auth/sign-in/email',{email:identity.email,password:identity.password});
    const me=await api<{user:User|null}>('/api/auth/get-session');
    if(!alive)return;
    if(me?.user){setError('');setUser(me.user);return;}
    setLoginRequested(true);
   }catch(signInError){
    if(!alive)return;
    if(!(signInError instanceof ApiError&&signInError.status===401)){setLoginRequested(true);return;}
    try{
     const created=await post<{user:User}>('/api/auth/sign-up/email',
      {email:identity.email,password:identity.password,name:identity.name});
     if(!alive)return;
     setError('');setUser(created.user);
    }catch{if(alive)setLoginRequested(true);}
   }
  })();
  return()=>{alive=false;};
 },[shell,loading,user,loginRequested]);
 useEffect(()=>{
  if(!user)return;const controller=new AbortController();
  void api<{items:PracticeQuestion[];assessment_mode:'scripted'|'live'}>('/api/catalog/questions',{signal:controller.signal}).then(result=>{if(!controller.signal.aborted){setQuestions(result.items);setQuestionId(result.items[0]?.id??'');setAssessmentMode(result.assessment_mode);}}).catch(e=>{if(!controller.signal.aborted)setError(describeError(e));});
  const id=new URLSearchParams(window.location.search).get('session');if(id&&/^[a-f0-9-]{36}$/.test(id))void open(id);
  void loadHistory(controller.signal).catch(e=>{if(!controller.signal.aborted)setError(describeError(e));});
  return()=>{controller.abort();epoch.current++;};
 },[user?.id]);
 async function start(){
  if(startLock.current||!questionId)return;startLock.current=true;setStarting(true);setError('');const current=++epoch.current;
  if(!startRequest.current||startRequest.current.questionId!==questionId)startRequest.current={questionId,targetKey:crypto.randomUUID(),sessionKey:crypto.randomUUID()};const command=startRequest.current;
  try{const target=await post<{id:string}>('/api/training-targets/',{role_package_id:'agent-intern',level:'intern',focus:questions.find(q=>q.id===questionId)?.topic??''},command.targetKey);
   const value=await post<PracticeSessionView>('/api/practice-sessions',{target_id:target.id,question_id:questionId},command.sessionKey);startRequest.current=null;
   if(current===epoch.current){setActive(value);window.history.replaceState(null,'',`/?session=${value.id}`);await loadHistory();}
  }catch(e){if(current===epoch.current)setError(describeError(e));}finally{startLock.current=false;setStarting(false);}
 }
 // F07Z-77: every account-scoped key this window holds, in one place. Signing out used to be
 // the only caller, so "switch account" left the previous account's pending command and answer
 // drafts sitting in the same window. Isolation is by key prefix, but leaving the text behind
 // is still the previous account's content outliving its session.
 function clearAccountState(userId:string|null|undefined){
  if(!userId)return;
  try{
   for(const key of Object.keys(sessionStorage))
    if(key.startsWith(`py-practice:${userId}:`)||key.startsWith(`py-career:${userId}:`)||
       key.startsWith(`py-interview:${userId}:`)||key.startsWith('py-interview-draft:'))
     sessionStorage.removeItem(key);
  }catch{/* storage may be blocked; the server boundary still applies */}
 }
 async function logout(){await post('/api/auth/sign-out',{});clearAccountState(user?.id);epoch.current++;startRequest.current=null;setUser(null);setActive(null);setHistory([]);setQuestions([]);window.history.replaceState(null,'','/');}
 const home=()=>{setWorkspace('practice');epoch.current++;setActive(null);setError('');window.history.replaceState(null,'','/');};
 // Desktop entry: the same auth, goal and interview logic, presented by the desktop
 // shell instead of the web header/workspace. `shell` is resolved once per window, so a
 // reload or a rewritten URL cannot switch entry points mid-session.
 if(shell==='desktop'){
  // loginRequested wins even while a user is signed in: that is the "switch account" flow.
  if(loading||( !user&&!loginRequested))return <div className="desktop-shell is-booting" data-theme={activeTheme}><WindowStrip/><p role="status" className="desktop-loading">正在进入工作台…</p></div>;
  if(!user||loginRequested)return <div className="desktop-shell is-login" data-theme={activeTheme}><WindowStrip/><div className="desktop-login">
   {error&&<p role="alert" className="error desktop-login-error">{error}</p>}
   <AuthPanel variant="desktop" onLogin={value=>{setError('');setLoginRequested(false);if(user&&user.id!==value.id)clearAccountState(user.id);setUser(value);}}/>
   <p className="fine-print desktop-login-alt">只是想先看看？
    <button type="button" className="text-button" onClick={()=>{setError('');setLoginRequested(false);}}>使用本机身份进入工作台</button></p>
  </div></div>;
  return <CareerPanel key={user.id} variant="desktop" userId={user.id} assessmentMode={assessmentMode} accountName={user.name} accountEmail={user.email}
   onAuthLost={authLost} onSwitchAccount={()=>{clearAccountState(user?.id);setLoginRequested(true);}}
   appearance={activeTheme} appearanceChoice={theme} onAppearanceChange={changeTheme}
   onLogout={()=>{setLoginRequested(true);void logout().catch(e=>setError(describeError(e)));}}/>;
 }
 return <><header className="site-header"><button className="brand" onClick={home}><span className="brand-mark">习</span><span>练习间<small>INTERVIEW PRACTICE</small></span></button>{user?<div className="account"><button className="text-button" onClick={()=>{epoch.current++;setActive(null);setWorkspace('career');window.history.replaceState(null,'','/');}}>求职目标</button><button className="text-button" onClick={()=>{epoch.current++;setActive(null);setWorkspace('career');window.history.replaceState(null,'','/?view=interview');window.setTimeout(()=>document.getElementById('interview-workspace')?.scrollIntoView({block:'start',behavior:'smooth'}),0);}}>整场面试</button><button className="text-button" onClick={home}>单题练习</button><span>{user.name}</span><button className="text-button" onClick={()=>void logout().catch(e=>setError(describeError(e)))}>退出登录</button></div>:<span className="header-note">每一次回答，都值得回看</span>}</header><main>{loading?<p role="status" className="loading">正在读取练习空间…</p>:!user?<>{error&&<p role="alert" className="error page-error auth-error">{error}</p>}<AuthPanel onLogin={value=>{setError('');setUser(value);}}/></>:<><div className="notice" hidden={workspace==='career'}>{assessmentMode==='live'?'AI反馈预览 · 无需简历或JD · 模型反馈仅供练习，不作正式评分':'脚本流程演示 · 无需简历或JD · 当前反馈不代表真实模型评价'}</div>{error&&<p role="alert" className="error page-error">{error}</p>}{workspace==='career'?<CareerPanel key={user.id} userId={user.id} assessmentMode={assessmentMode} onAuthLost={authLost}/>:session?<TrainingRoom key={session.id} user={user} session={session} onRefresh={refresh} onNew={home} onAuthLost={authLost}/>:<div className="workspace"><aside><span className="eyebrow">YOUR PATH</span><h2>岗位能力训练</h2><div className="role-tile"><span className="role-icon">↗</span><strong>AI 应用 / Agent 开发</strong><p>实习 · 基础与工程场景</p><span className="tag">无需上传材料</span></div><div className="recent"><h3>最近的练习</h3>{history.length===0?<p className="muted">完成第一题后，会保存在这里。</p>:history.slice(0,8).map(item=><button className="history-item" key={item.id} onClick={()=>void open(item.id)}><span>{item.question.text}</span><small>{phase[item.status]??item.status}</small></button>)}</div></aside><section className="card setup-card"><span className="eyebrow">A SMALL STEP, EVERY TIME</span><h1>今天，先练好一题。</h1><p className="lead">把“我知道”变成能讲清楚的回答。<br/>从岗位基础开始，按自己的节奏练习。</p><div className="setup-divider"/><label htmlFor="role">目标岗位</label><select id="role" value="agent-intern" onChange={()=>{}}><option value="agent-intern">AI 应用 / Agent 开发实习</option></select><label htmlFor="question">选择练习问题</label><select id="question" value={questionId} onChange={e=>setQuestionId(e.target.value)}>{questions.map(q=><option key={q.id} value={q.id}>{topics[q.topic]??q.topic} · {q.text.slice(0,38)}{q.text.length>38?'…':''}</option>)}</select><div className="question-preview">{questions.find(q=>q.id===questionId)?.text??'正在读取问题…'}</div><button className="primary" disabled={starting||questions.length===0} onClick={()=>void start()}>{starting?'正在准备练习…':'开始这一题 →'}</button><div className="expectations"><span>独立作答</span><span>有依据的反馈</span><span>回答与反馈可回看</span></div></section></div>}</>}</main><footer>练习间 · AI 模拟训练，不代表真实招聘结论</footer></>;
}
