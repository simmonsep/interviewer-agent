import {useEffect,useRef,useState,type FormEvent} from 'react';
import {api,post,ApiError} from './api.ts';
import {isProcessing,isTerminal,latestQuestion,questionText,formatRemaining,statusLabel,stopReasonLabel,runErrorLabel,
  type BankSummary,type InterviewSession,type AnswerPayload,type ExplanationPayload,type QuestionPayload} from './interviewContracts.ts';
import {getInterviewReviewPresentation,type ReviewRetest} from './reviewPresentation.ts';
import {getAsyncActionCopy} from './asyncActionCopy.ts';
import {OperationErrorDetails,errorDetailOf,type OperationErrorDetail} from './OperationErrorDetails.tsx';
import {InterviewReview} from './InterviewReview.tsx';
import {notifyDataChanged} from './dataEvents.ts';

// sessionId pins a pending replay to the session that produced it. It is optional so
// commands written by older builds (only the URL is available) can still be resolved.
interface Command {kind:'create'|'answer';url:string;key:string;body:Record<string,unknown>;sessionId?:string}
interface GoalSnapshot {version:number;status:string}
// Compact, display-only snapshot for the desktop context bar. The panel stays the only
// owner of polling, submission and the pending-command state machine.
export interface InterviewContext{
 sessionId:string|null;status:string|null;questionIndex:number;maxQuestions:number;
 deadlineAt:number;pending:boolean;terminal:boolean;
 history:{id:string;status:string;mode:string;questions:number;answers:number;bank:string}[];
}
interface Props {
 userId:string;goalId:string;goalVersion:number;goalStatus:string;onAuthLost:()=>void;onGoalRefresh?:()=>void;
 variant?:'web'|'desktop';
 // Version B: the shell pins one of the two frozen modes when the user enters
 // from an explicit Agent interface. The web entry keeps the original default.
 agentMode?:'interview'|'coaching';
 // F07Z-67: one 面试 entry. The setup card owns the choice, so the shell has to accept it back;
 // without the callback (web variant) the switch is not rendered and the prop stays authoritative.
 onAgentModeChange?:(mode:'interview'|'coaching')=>void;
 // F07Z-61: the setup copy must not claim "free scripted" while the API is running live.
 assessmentMode?:'scripted'|'live';
 onContext?:(value:InterviewContext)=>void;
 openSessionId?:string|null;
 onOpenHandled?:()=>void;
 /** F07Z-142: after a session ends the same page can hand over to the next step (applications). */
 onOpenApplications?:()=>void;
 /** F07Z-150: the shell owns navigation, so history rows use the same route as everything else. */
 onOpenSession?:(id:string)=>void;
 /** F07Z-143: the confirmed target name (job title when one is selected), shown on the review. */
 goalTitle?:string|null;
 /** F07Z-151: the selected position's key words, so a session is visibly aimed at that job. */
 jobKeyPoints?:{title:string;skills:string[]}|null;
 /** F07Z-143: a投递 needs a selected job; the review explains that instead of a dead end. */
 hasSelectedJob?:boolean;
}

const POLL_INTERVAL_MS=1500;
const MAX_POLLS=60;
const MAX_ANSWER_LENGTH=1500;
const UUID=/^[a-f0-9-]{36}$/i;

// 2.3: every account-scoped interview cache key starts with this prefix, so a successful
// sign-out can remove the session pointer, the pending replay and all answer drafts at once.
function storagePrefix(userId:string){return `py-interview:${userId}:`;}
function draftKey(userId:string,sessionId:string,questionId:string){
 return `${storagePrefix(userId)}draft:${sessionId}:${questionId}`;
}
// Legacy key has no user id. It is only read as a fallback and removed on sign-out.
function legacyDraftKey(sessionId:string,questionId:string){return `py-interview-draft:${sessionId}:${questionId}`;}
function readDraft(userId:string,sessionId:string,questionId:string):string{
 try{return sessionStorage.getItem(draftKey(userId,sessionId,questionId))
  ??sessionStorage.getItem(legacyDraftKey(sessionId,questionId))??'';}catch{return '';}
}
function writeDraft(userId:string,sessionId:string,questionId:string,value:string){
 try{sessionStorage.setItem(draftKey(userId,sessionId,questionId),value);}catch{}
}
function clearDraft(userId:string,sessionId:string,questionId:string){
 try{sessionStorage.removeItem(draftKey(userId,sessionId,questionId));
  sessionStorage.removeItem(legacyDraftKey(sessionId,questionId));}catch{}
}
function answerEvents(session:InterviewSession|null,questionId:string){
 return session?.events.filter(event=>event.kind==='answer'
  &&(event.payload as AnswerPayload).question_id===questionId)??[];
}
function answerForQuestion(session:InterviewSession|null,questionId:string):boolean{
 return answerEvents(session,questionId).length>0;
}
// Only claim "this submission took effect" when a NEW answer event appeared after the
// request, matching both the question event id and the exact text of this submission.
function provesSubmission(before:InterviewSession|null,after:InterviewSession|null,command:Command):boolean{
 if(!after||command.kind!=='answer')return false;
 const questionId=String(command.body.question_id),text=String(command.body.text);
 const known=new Set((before?.events??[]).map(event=>event.id));
 return after.events.some(event=>event.kind==='answer'&&!known.has(event.id)
  &&(event.payload as AnswerPayload).question_id===questionId
  &&(event.payload as AnswerPayload).text===text);
}
function deadlineTime(state:InterviewSession|null):number{
 const value=state?new Date(state.deadline_at).getTime():0;
 return Number.isFinite(value)&&value>0?value:0;
}
// 2.2: checked again inside the handlers, independent of the 1-second UI clock.
function deadlineExpired(state:InterviewSession|null):boolean{
 const value=deadlineTime(state);
 return value>0&&Date.now()>=value;
}
// The pending command targets ONE session. Never judge its deadline by whichever
// session happens to be open: use the id recorded on the command (or parse the original
// URL for commands persisted by older builds).
function commandSessionId(command:Command):string{
 if(command.sessionId&&UUID.test(command.sessionId))return command.sessionId;
 const match=command.url.match(/\/interviews\/([^/]+)\/answers(?:\?|$)/);
 return match?.[1]??'';
}
// F07Z-65: a paused or failed row must name the reason. `status` alone says "后台已暂停" even
// when the run already knows it was a budget or credential problem.
function historyStatus(item:InterviewSession):string{
 const code=item.run?.error_code;
 if((item.status==='WAITING_CREDENTIALS'||item.status==='FAILED')&&code)
  return `${statusLabel[item.status]??item.status} · ${runErrorLabel[code]??code}`;
 return statusLabel[item.status]??item.status;
}
type ReplayCheck='ready'|'expired'|'closed'|'other-session'|'unknown-deadline';
// Single gate shared by the button and retry(). The deadline is taken from the detail
// of the TARGET session only, and must be a valid server-provided value.
function replayCheck(command:Command|null,state:InterviewSession|null):ReplayCheck{
 if(!command||command.kind!=='answer')return 'ready';
 const targetId=commandSessionId(command);
 if(!targetId||!state||state.id!==targetId)return 'other-session';
 const time=deadlineTime(state);
 if(time<=0)return 'unknown-deadline';
 if(Date.now()>=time)return 'expired';
 if(isTerminal(state.status))return 'closed';
 return 'ready';
}
function replayBlockedMessage(check:ReplayCheck,targetId:string):string{
 if(check==='expired')return '原场次已到服务端 deadline，答题重放已停用；结束原因以服务端终态为准。';
 if(check==='closed')return '原场次已结束，答题重放已停用；请刷新状态核对服务端终态。';
 if(check==='unknown-deadline')return '无法确认原场次的服务端截止时间，请点「刷新状态」或打开原场次核对后再重放。';
 return `待重放请求属于原场次 ${targetId.slice(0,8)}；当前打开的是其他场次，或原场次详情尚未加载。请先打开原场次核对截止时间，请求与草稿已保留。`;
}
type Failure='conflict'|'in_progress'|'unknown'|'blocked';
function classifyFailure(error:unknown):Failure{
 if(!(error instanceof ApiError))return 'unknown';           // network abort / lost response
 if([401,403,404].includes(error.status))return 'blocked';
 if(error.code==='version_conflict'||error.code==='question_changed')return 'conflict';
 if(error.code==='interview_in_progress')return 'in_progress';
 if(error.status>=500)return 'unknown';
 return 'blocked';
}
function interviewError(error:unknown):string{
 if(error instanceof ApiError){
  const byCode:Record<string,string>={
   version_conflict:'服务端状态已变化（版本冲突）。旧请求不能重放，已回读最新状态。',
   question_changed:'题目已变化。旧回答按原题保存，请用当前题目重新作答。',
   idempotency_conflict:'同一幂等键已用于不同请求，请刷新页面后重新操作。',
   interview_in_progress:'该目标已有进行中的面试，请从历史记录打开原场次。',
   interview_session_limit:'该目标的面试场次已达上限。',
   question_bank_disabled:'该题库已停用，请重新选择已启用题库。',
   question_bank_role_not_supported:'该题库没有适用于 AI 应用岗位的题目。',
   idempotency_key_required:'请求缺少有效的幂等键，请刷新页面重试。',
   authentication_required:'登录已过期，请重新登录。',
   resource_unavailable:'这场面试已不可用，或不属于当前账号。'
  };
  if(byCode[error.code])return byCode[error.code];
  if(error.status===401)return '登录已过期，请重新登录。';
  if(error.status===403)return '当前请求未获允许，请从求职目标页面重新进入。';
  if(error.status===404)return '目标或面试场次不存在，请刷新后重试。';
  if(error.status===409)return '状态已变化，正在回读最新记录；请勿用旧请求重放。';
  if(error.status===400||error.status===422)return '请检查输入：回答需非空且不超过 1500 字。';
 }
 return '请求未完成。你的输入仍保留，请先刷新状态核对。';
}
function bankLabel(bank:BankSummary):string{
 return `${bank.name} · v${bank.version} · ${bank.origin==='builtin'?'内置':'私有'} · ${bank.item_count} 题`;
}

export function InterviewPanel({userId,goalId,goalVersion,goalStatus,onAuthLost,onGoalRefresh,variant='web',agentMode='interview',onAgentModeChange,assessmentMode='scripted',onContext,openSessionId,onOpenHandled,onOpenApplications,onOpenSession,goalTitle,hasSelectedJob,jobKeyPoints}:Props){
 const desktop=variant==='desktop';
 const base=`/api/career-goals/${goalId}/interviews`;
 const storageKey=`${storagePrefix(userId)}${goalId}`;
 const currentKey=`${storageKey}:current`,pendingKey=`${storageKey}:pending`;
 const sectionRef=useRef<HTMLElement|null>(null);
 const [banks,setBanks]=useState<BankSummary[]>([]),[bankChoice,setBankChoice]=useState('');
 const [withMistakes,setWithMistakes]=useState(false);
 const [session,setSession]=useState<InterviewSession|null>(null),[history,setHistory]=useState<InterviewSession[]>([]);
 const [text,setText]=useState(''),[error,setError]=useState(''),[notice,setNotice]=useState(''),[busy,setBusy]=useState(false);
 // F07Z-146: the API code lives behind a collapsed disclosure, never in the sentence itself.
 const [errorDetail,setErrorDetail]=useState<OperationErrorDetail|null>(null);
 // F07Z-150: a deep link can point at a session that does not exist (or is not ours). That is a
 // route problem, not a form error, so it gets its own block with a way back.
 const [routeMissing,setRouteMissing]=useState(false);
 const [now,setNow]=useState(()=>Date.now()),[pendingVisible,setPendingVisible]=useState(false);
 // F07Z-167: the per-question soft reminder is display-only; it never calls the model.
 const [remind,setRemind]=useState(false);
 const [pendingKind,setPendingKind]=useState<Command['kind']|null>(null);
 const [conflict,setConflict]=useState<Command['kind']|null>(null);
 const [savedAnswer,setSavedAnswer]=useState(false),[activeSessionId,setActiveSessionId]=useState<string|null>(null);
 const [goalSnapshot,setGoalSnapshot]=useState<GoalSnapshot>({version:goalVersion,status:goalStatus});
 const current=useRef<InterviewSession|null>(null),epoch=useRef(0),sequence=useRef(0),lock=useRef(false);
 const pending=useRef<Command|null>(null),goalRef=useRef(goalSnapshot);goalRef.current=goalSnapshot;
 const refreshRef=useRef<()=>Promise<void>>(async()=>{}),expiredRef=useRef<string|null>(null);
 const accept=(value:InterviewSession|null)=>{current.current=value;setSession(value);};
 const clearPending=()=>{pending.current=null;setPendingVisible(false);setPendingKind(null);
  try{sessionStorage.removeItem(pendingKey);}catch{}};
 const storePending=(command:Command)=>{pending.current=command;setPendingVisible(true);setPendingKind(command.kind);
  try{sessionStorage.setItem(pendingKey,JSON.stringify(command));}catch{}};
 useEffect(()=>{setGoalSnapshot({version:goalVersion,status:goalStatus});},[goalVersion,goalStatus]);

 async function loadBanks(gen=epoch.current){
  const value=await api<{banks:BankSummary[]}>('/api/question-banks');
  if(gen!==epoch.current)return;
  const usable=value.banks.filter(bank=>bank.enabled);
  setBanks(usable);
  setBankChoice(previous=>{
   if(usable.some(bank=>`${bank.bank_id}|${bank.version}`===previous))return previous;
   const first=usable.find(bank=>bank.origin==='private')??usable[0];
   return first?`${first.bank_id}|${first.version}`:'';
  });
 }
 async function loadHistory(gen=epoch.current):Promise<InterviewSession[]>{
  const value=await api<{items:InterviewSession[]}>(base);
  if(gen!==epoch.current)return [];
  setHistory(value.items);return value.items;
 }
 async function refresh(gen=epoch.current,silent=false){
  const before=current.current;if(!before)return;
  const seq=++sequence.current;
  try{
   const value=await api<InterviewSession>(`${base}/${before.id}`);
   if(gen!==epoch.current||seq!==sequence.current||current.current?.id!==value.id)return;
   if(value.version>=(current.current?.version??0))accept(value);
   if(isTerminal(value.status)||value.status!==before.status||value.revision!==before.revision)await loadHistory(gen).catch(()=>[]);
  }catch(e){
   if(gen===epoch.current&&seq===sequence.current){
    if(e instanceof ApiError&&e.status===401)onAuthLost();
    else if(!silent)setError(interviewError(e));
    throw e;
   }
  }
 }
 refreshRef.current=()=>refresh(epoch.current,true);
 async function openSession(id:string,gen=++epoch.current){
  setError('');setNotice('');setConflict(null);setSavedAnswer(false);setActiveSessionId(null);
  accept(null);setText('');
  try{
   const value=await api<InterviewSession>(`${base}/${id}`);
   if(gen!==epoch.current)return;
   accept(value);sessionStorage.setItem(currentKey,id);setRouteMissing(false);
  }catch(e){
   if(gen!==epoch.current)return;
   if(e instanceof ApiError&&e.status===401)onAuthLost();
   else if(e instanceof ApiError&&e.status===404)setRouteMissing(true);
   else setError(interviewError(e));
  }
 }
 // A confirmed version conflict cannot be solved by replaying the old body: the server
 // compares expected_version / the latest question event id. Keep the user in control and
 // let the next explicit action build a NEW body with the latest version and a NEW key.
 async function handleConflict(command:Command,before:InterviewSession|null,after:InterviewSession|null,gen:number){
  clearPending();
  if(command.kind==='create'){
   let snapshot:GoalSnapshot|null=null;
   try{
    const value=await api<GoalSnapshot>(`/api/career-goals/${goalId}`);
    if(gen!==epoch.current)return;
    snapshot={version:Number(value.version),status:String(value.status)};
   }catch{if(gen!==epoch.current)return;}
   if(gen!==epoch.current)return;
   if(snapshot){setGoalSnapshot(snapshot);onGoalRefresh?.();}
   const active=(await loadHistory(gen).catch(()=>[] as InterviewSession[])).find(item=>!isTerminal(item.status));
   if(active)setActiveSessionId(active.id);
   setConflict('create');
   if(!snapshot){
    setNotice('开场遇到版本冲突，目标版本回读失败。请点目标卡的「刷新目标」或刷新页面，核对最新目标后再开场；题库选择已保留。');
   }else{
    setNotice(`目标版本已更新到 v${snapshot.version}，题库选择已保留。请核对目标卡后，再点「开始整场面试」——将使用最新目标版本与新幂等键，不会重放旧请求。`
     +(active?'该目标存在进行中的场次，可先打开原场次。':''));
   }
   return;
  }
  const questionId=String(command.body.question_id);
  if(!after){
   setNotice('答题遇到版本冲突，但场次回读失败。请点「刷新状态」或刷新页面核对最新题目与版本；输入已保留。');
   return;
  }
  if(provesSubmission(before,after,command)){
   clearDraft(userId,after.id,questionId);setText('');
   setNotice('已回读确认：本次提交已生效（服务端已保存本次回答）。请勿重复提交。');
   return;
  }
  if(answerForQuestion(after,questionId)){
   setConflict('answer');setSavedAnswer(true);
   setNotice('该题已有已保存回答，但证据不足以确认来自本次提交。已展示当前状态，请勿再次提交；可点「刷新状态」核对。');
   return;
  }
  const latest=latestQuestion(after);
  if(latest&&latest.id!==questionId){
   if(text.trim())writeDraft(userId,after.id,questionId,text);
   setConflict(null);setSavedAnswer(false);
   setText(readDraft(userId,after.id,latest.id));   // new question never inherits the old answer
   setNotice('题目已变化：旧回答已按原题事件 ID 保存，当前输入框不会带入旧答案。请作答新题。');
   return;
  }
  setConflict('answer');setSavedAnswer(false);
  if(after.status!=='WAITING_FOR_USER'){
   setNotice(`会话已更新到 v${after.version}，当前状态为「${statusLabel[after.status]??after.status}」，暂不能作答。请回读状态后再提交。`);
  }else{
   setNotice(`会话版本已更新到 v${after.version}，题目未变、原文已保留。请核对后点「提交这一答 →」：将使用最新会话版本与新幂等键，旧幂等键已废弃。`);
  }
 }
 // Single sender. Confirmed conflicts discard the pending command; only an UNKNOWN result
 // keeps the exact URL/body/idempotency-key triple for a manual replay. No automatic resend.
 async function runCommand(command:Command,gen:number):Promise<boolean>{
  const before=current.current;
  try{
   const value=await post<InterviewSession>(command.url,command.body,command.key);
   clearPending();
   if(gen!==epoch.current)return false;
   accept(value);sessionStorage.setItem(currentKey,value.id);
   setError('');setErrorDetail(null);setNotice('');setConflict(null);setSavedAnswer(false);
   await loadHistory(gen).catch(()=>[]);
   return true;
  }catch(e){
   if(gen!==epoch.current)return false;
   if(e instanceof ApiError&&e.status===401){onAuthLost();return false;}
   setError(interviewError(e));setErrorDetail(errorDetailOf(e));
   let readOk=true;
   if(current.current)await refresh(gen,true).catch(()=>{readOk=false;});
   const items=await loadHistory(gen).catch(()=>{readOk=false;return [] as InterviewSession[];});
   if(gen!==epoch.current)return false;
   const after=current.current,kind=classifyFailure(e);
   if(kind==='conflict'){await handleConflict(command,before,after,gen);return false;}
   if(kind==='in_progress'){
    clearPending();
    const active=items.find(item=>!isTerminal(item.status));
    if(active){accept(active);sessionStorage.setItem(currentKey,active.id);setActiveSessionId(null);
     setNotice('该目标已有进行中的面试，已打开原场次（没有创建新场次，也不重放旧请求）。');}
    else setNotice('回读不到进行中的场次。请点「刷新历史」或刷新页面核对后再操作。');
    return false;
   }
   if(kind==='blocked'){
    clearPending();
    setNotice(`${interviewError(e)} 请刷新状态或页面后重新操作；页面不会用旧请求自动重发。`);
    return false;
   }
   // Unknown outcome: read back first, then decide whether the POST actually landed.
   const questionId=command.kind==='answer'?String(command.body.question_id):'';
   if(questionId&&provesSubmission(before,after,command)){
    clearPending();clearDraft(userId,after!.id,questionId);setText('');
    setNotice('已回读确认：本次提交已生效。请勿重复提交，后台会继续生成下一题或结束本场。');
    return false;
   }
   if(questionId&&answerForQuestion(after,questionId)){
    clearPending();setSavedAnswer(true);
    setNotice('该题已有已保存回答，但无法确认是否来自本次提交。请核对当前状态，避免再次提交。');
    return false;
   }
   setSavedAnswer(false);
   setNotice(readOk
    ?'这次请求的结果未确认（响应丢失或网络中断）。输入已保留；请先刷新状态核对，再用「用同一请求重试」以原 URL、原 body、原幂等键重放，页面不会自动重发。'
    :'这次请求的结果未确认，且状态回读失败。请点「刷新状态」「刷新历史」或刷新页面核对；确认未生效后可用「用同一请求重试」重放原请求。');
   return false;
  }
 }
 async function start(){
  if(lock.current||!bankChoice||goalRef.current.status==='CANCELLED'||pendingVisible)return;
  const [bankId,rawVersion]=bankChoice.split('|');const version=Number(rawVersion);
  if(!bankId||!Number.isInteger(version))return;
  lock.current=true;setBusy(true);setError('');setNotice('');setConflict(null);
  const gen=++epoch.current;
  // A fresh action always uses the latest known goal version and a NEW idempotency key.
  const body={expected_version:goalRef.current.version,role:'ai-application',mode:agentMode,
   max_questions:2,duration_seconds:3600,question_banks:[{bank_id:bankId,version}],
   ...(withMistakes?{include_practice_mistakes:true}:{})};
  const command:Command={kind:'create',url:base,key:crypto.randomUUID(),body};
  storePending(command);
  await runCommand(command,gen);
  lock.current=false;setBusy(false);
 }
 async function startFocus(item:ReviewRetest){
  const state=current.current;
  if(lock.current||pendingVisible||!state||state.status!=='COMPLETED')return;
  lock.current=true;setBusy(true);setError('');setNotice('');setConflict(null);
  const gen=++epoch.current;
  // F07Z-165 A: the server re-checks the source observation and freezes the question text.
  const body={expected_version:goalRef.current.version,role:state.policy.role,mode:'coaching',
   max_questions:2,duration_seconds:3600,
   question_banks:state.question_banks.map(bank=>({bank_id:bank.bank_id,version:bank.version})),
   focus_session_id:state.id,focus_observation_id:item.observationId};
  const command:Command={kind:'create',url:base,key:crypto.randomUUID(),body};
  storePending(command);
  const ok=await runCommand(command,gen);
  if(ok)setNotice('已按这条建议开一场定向陪练；首题就是建议题，作答记在新场，不改原场记录。');
  lock.current=false;setBusy(false);
 }
 async function submit(event:FormEvent){
  event.preventDefault();
  const state=current.current,question=latestQuestion(state);
  if(lock.current||!state||!question||state.status!=='WAITING_FOR_USER')return;
  if(deadlineExpired(state)){
   setError('本场已到服务端 deadline，已停止答题提交；结束原因以服务端终态为准。');
   void refresh(epoch.current,true).catch(()=>{});
   return;
  }
  if(!text.trim()||text.length>MAX_ANSWER_LENGTH){setError('回答不能为空，且最多 1500 字。');return;}
  if(pendingVisible){setError('存在未确认的请求，请先用「用同一请求重试」或刷新状态核对，避免重复提交。');return;}
  if(savedAnswer){setError('该题已有已保存回答，请先刷新状态核对，不要再次提交。');return;}
  lock.current=true;setBusy(true);setError('');setNotice('');setConflict(null);
  const body={expected_version:state.version,question_id:question.id,text};
  const command:Command={kind:'answer',url:`${base}/${state.id}/answers`,key:crypto.randomUUID(),body,sessionId:state.id};
  storePending(command);
  const ok=await runCommand(command,epoch.current);
  if(ok){clearDraft(userId,state.id,question.id);setText('');}
  lock.current=false;setBusy(false);
 }
 async function retry(){
  const command=pending.current;if(!command||lock.current)return;
  // Re-check at send time, using the SAME gate as the button and the target session
  // recorded on the command. Viewing another (unexpired) session or a not-yet-loaded
  // detail must never unlock a replay aimed at an expired session.
  if(command.kind==='answer'){
   const check=replayCheck(command,current.current);
   if(check!=='ready'){
    if(check==='expired'||check==='closed'){
     setError('本场已到服务端 deadline 或已结束，已停止答题重放；结束原因以服务端终态为准。');
     void refresh(epoch.current,true).catch(()=>{});
    }else{
     setError(check==='unknown-deadline'
      ?'无法确认原场次的服务端截止时间，请刷新状态或打开原场次核对后再重放；请求与草稿已保留。'
      :`待重放请求属于原场次 ${commandSessionId(command).slice(0,8)}。请先打开原场次并确认服务端截止时间后再重放；请求与草稿已保留。`);
    }
    return;
   }
  }
  lock.current=true;setBusy(true);setError('');setNotice('');
  const ok=await runCommand(command,epoch.current);
  if(ok&&command.kind==='answer'){
   // Clear the draft of the ORIGINAL session and original question, not the one on screen.
   const targetId=commandSessionId(command),questionId=String(command.body.question_id);
   if(targetId)clearDraft(userId,targetId,questionId);
   setText('');
  }
  lock.current=false;setBusy(false);
 }
 function reset(){
  // Switching sessions is not a destructive action: keep the pending command and its
  // drafts. Only the "current session" pointer is dropped so the setup form can show.
  const gen=++epoch.current;accept(null);setText('');setError('');setNotice('');
  setConflict(null);setSavedAnswer(false);setActiveSessionId(null);
  try{sessionStorage.removeItem(currentKey);}catch{}
  void loadHistory(gen).catch(()=>[]);
 }
 // "另开一场" is only offered on a server-confirmed terminal session. A stale pending
 // ANSWER command for that very session can never take effect, so keeping it would block
 // start()/the setup button forever. Clear it ONLY when the open detail IS the command's
 // target session AND the SERVER (not a local countdown) marked that session terminal.
 // A local deadline of zero, an unloaded or failed detail, and another terminal session
 // (B) all keep A's command and its original-question draft untouched.
 function startAnother(){
  const command=pending.current,state=current.current;
  const obsolete=command?.kind==='answer'&&!!state&&state.id===commandSessionId(command)
   &&isTerminal(state.status);
  if(obsolete)clearPending();
  // F07Z-65: restore the old setup so "再开一场" costs one click and keeps the same intent.
  // Policy is authoritative (the first question may never have been generated); a question
  // event is only a fallback for older sessions.
  const bank=state?.question_banks?.[0];
  const mistakes=Boolean(state?.policy?.include_practice_mistakes)
   ||Boolean(state?.events?.some(event=>event.kind==='question'
    &&(event.payload as QuestionPayload).origin==='practice_record'));
  reset();
  if(bank)setBankChoice(`${bank.bank_id}|${bank.version}`);
  setWithMistakes(mistakes);
  if(obsolete)setNotice(
   '原场次已由服务端结束，其待重放答题请求已清除；原题目草稿仍保留。请自行核对后点「开始整场面试 →」开新场，页面不会自动开场。');
  else if(bank||mistakes)setNotice(
   '已按上一场恢复题库与练习错题勾选；请确认后点「开始整场面试 →」，页面不会自动开场。');
 }
 // F07Z-65: a paused or timed-out session has no way forward unless it can be ended. The
 // backend has always supported it; this exposes it and refuses to do anything else.
 async function stopSession(){
  const state=current.current;
  if(lock.current||!state||isTerminal(state.status))return;
  if(!window.confirm('结束本场？已保存的回答与讲解会保留在历史中。'))return;
  lock.current=true;setBusy(true);setError('');setNotice('');
  const gen=epoch.current;
  try{
   const value=await post<InterviewSession>(`${base}/${state.id}/stop`,
    {expected_version:state.version},crypto.randomUUID());
   if(gen!==epoch.current)return;
   accept(value);
   // A pending ANSWER for this very session can never take effect now; keeping it would
   // block the setup button of the next session.
   if(pending.current&&commandSessionId(pending.current)===state.id)clearPending();
   setNotice('本场已结束（用户结束）。已保存的回答与讲解保留在历史中。');
   await loadHistory(gen).catch(()=>[]);
  }catch(e){
   if(gen===epoch.current){
    if(e instanceof ApiError&&e.status===401)onAuthLost();
    else setError(interviewError(e));
   }
  }finally{lock.current=false;setBusy(false);}
 }
 // F07Z-167: skip the current question. The server records a skip event with no observation,
 // counts the round as asked and asks the interviewer for the next question; the round is
 // never marked wrong. A racing answer and skip let only the first commit win (409).
 async function skipQuestion(){
  const state=current.current;
  if(lock.current||!state||state.status!=='WAITING_FOR_USER')return;
  const currentQuestion=latestQuestion(state);
  if(!currentQuestion)return;
  lock.current=true;setBusy(true);setError('');setNotice('');
  const gen=epoch.current;
  try{
   const value=await post<InterviewSession>(`${base}/${state.id}/skip`,
    {expected_version:state.version,question_id:currentQuestion.id},crypto.randomUUID());
   if(gen!==epoch.current)return;
   accept(value);
   clearDraft(userId,state.id,currentQuestion.id);
   setNotice('已跳过本题：这一轮记为未作答，不会标成答错；面试官正在选下一题。');
   await loadHistory(gen).catch(()=>[]);
  }catch(e){
   if(gen===epoch.current){
    if(e instanceof ApiError&&e.status===401)onAuthLost();
    else setError(interviewError(e));
   }
  }finally{lock.current=false;setBusy(false);}
 }
 // F07Z-167: clarify restates the frozen scope only. The server keeps the same question and
 // version, makes no model call and does not add a round.
 async function clarifyQuestion(){
  const state=current.current;
  if(lock.current||!state||state.status!=='WAITING_FOR_USER')return;
  const currentQuestion=latestQuestion(state);
  if(!currentQuestion)return;
  lock.current=true;setBusy(true);setError('');setNotice('');
  const gen=epoch.current;
  try{
   const value=await post<InterviewSession>(`${base}/${state.id}/clarify`,
    {expected_version:state.version,question_id:currentQuestion.id},crypto.randomUUID());
   if(gen!==epoch.current)return;
   accept(value);
   setNotice('已重述题目范围：题目、题位和计时都不变；澄清不提供答案或提示。');
  }catch(e){
   if(gen===epoch.current){
    if(e instanceof ApiError&&e.status===401)onAuthLost();
    else setError(interviewError(e));
   }
  }finally{lock.current=false;setBusy(false);}
 }
 // The bank pane can enable or disable a bank; the list here has to follow without a reload.
 useEffect(()=>{
  const onChange=()=>void loadBanks().catch(()=>{});
  window.addEventListener('py-banks-changed',onChange);
  return()=>window.removeEventListener('py-banks-changed',onChange);
 },[]);
 useEffect(()=>{
  const gen=++epoch.current;
  void loadBanks(gen).catch(e=>{if(gen===epoch.current)setError(interviewError(e));});
  void loadHistory(gen).catch(e=>{if(gen===epoch.current)setError(interviewError(e));});
  try{const saved=JSON.parse(sessionStorage.getItem(pendingKey)??'null') as Command|null;
   if(saved?.key&&saved.url&&saved.body&&(saved.kind==='create'||saved.kind==='answer')){
    pending.current=saved;setPendingVisible(true);setPendingKind(saved.kind);}}
  catch{clearPending();}
  const saved=sessionStorage.getItem(currentKey);
  if(saved&&UUID.test(saved))void openSession(saved,gen);
  if(new URLSearchParams(window.location.search).get('view')==='interview')
   window.setTimeout(()=>sectionRef.current?.scrollIntoView({block:'start',behavior:'smooth'}),0);
  return()=>{epoch.current++;};
 },[goalId,userId]);
 // Limited polling only while the server is producing the next question; stops on unmount or terminal state.
 useEffect(()=>{
  if(!session||!isProcessing(session.status))return;
  let alive=true,ticks=0;
  const timer=window.setInterval(()=>{
   if(!alive)return;ticks++;
   void refreshRef.current().catch(()=>{});
   if(ticks>=MAX_POLLS){alive=false;window.clearInterval(timer);
    setNotice(previous=>previous||'后台仍在处理，已停止自动刷新。可点「刷新状态」继续核对。');}
  },POLL_INTERVAL_MS);
  return()=>{alive=false;window.clearInterval(timer);};
 },[session?.id,session?.status]);
 useEffect(()=>{
  const timer=window.setInterval(()=>setNow(Date.now()),1000);
  return()=>window.clearInterval(timer);
 },[]);

 const question=latestQuestion(session),questionId=question?.id??'';
 const clarification=session?[...session.events].reverse().find(event=>event.kind==='clarification'
  &&((event.payload as {question_id?:string}).question_id??'')===questionId):null;
 // Drafts are scoped to user + session + question event; a new question never shows the old text.
 useEffect(()=>{
  if(!session||!questionId)return;
  setText(readDraft(userId,session.id,questionId));
 },[session?.id,questionId]);
 const deadline=deadlineTime(session);
 const remaining=deadline-now,expired=deadline>0&&remaining<=0;
 // F07Z-167: a soft, client-side reminder only. It makes no request and never changes the
 // question; the server's deadline still decides when answers are accepted.
 const questionKind=(question?.payload as QuestionPayload|undefined)?.kind;
 useEffect(()=>{
  setRemind(false);
  if(!questionId||session?.status!=='WAITING_FOR_USER')return;
  const timer=window.setTimeout(()=>setRemind(true),(questionKind==='project'?240:120)*1000);
  return()=>window.clearTimeout(timer);
 },[questionId,session?.status,questionKind]);
 const terminal=session?isTerminal(session.status):false;
 // The replay gate is evaluated against the command's own session, never the open one.
 const replayTargetId=pending.current?commandSessionId(pending.current):'';
 const replayGate=replayCheck(pending.current,session);
 const replayBlocked=pendingKind==='answer'&&replayGate!=='ready';
 // On expiry, read the server-side terminal state once (server owns the deadline).
 useEffect(()=>{
  if(!session||terminal||!expired)return;
  const token=`${session.id}:${session.version}`;
  if(expiredRef.current===token)return;
  expiredRef.current=token;
  void refresh(epoch.current,true).catch(()=>{});
 },[session?.id,session?.version,expired,terminal]);
 // Desktop only: publish a display snapshot upward and honour a one-shot request to
 // open a specific session. Neither path fetches, polls or submits on its own.
 const historySignature=history.map(item=>`${item.id}:${item.status}`).join(',');
 const questionEvents=session?session.events.filter(e=>e.kind==='question').length:0;
 useEffect(()=>{
  if(!desktop||!onContext)return;
  onContext({sessionId:session?.id??null,status:session?.status??null,questionIndex:questionEvents,
   maxQuestions:session?.policy.max_questions??0,deadlineAt:deadline,pending:pendingVisible,terminal,
   history:history.map(item=>({id:item.id,status:item.status,
    mode:item.policy.mode==='coaching'?'coaching':'interview',
    questions:item.events.filter(e=>e.kind==='question').length,
    answers:item.events.filter(e=>e.kind==='answer').length,
    bank:item.question_banks.map(b=>`${b.name} v${b.version}`).join('、')||'默认题库'}))});
 },[desktop,onContext,session?.id,session?.status,session?.version,questionEvents,deadline,pendingVisible,terminal,historySignature]);
 useEffect(()=>{
  if(!desktop||!openSessionId)return;
  if(current.current?.id===openSessionId){onOpenHandled?.();return;}
  void openSession(openSessionId).then(()=>onOpenHandled?.());
 },[desktop,openSessionId]);
 const report=session?.report??null;
 // F07Z-148: a finished session invalidates the rail badge, the workbench progress and the
 // recent-session list. One event, no full reload, no duplicated state.
 useEffect(()=>{
  if(session&&isTerminal(session.status))notifyDataChanged('interview');
 },[session?.id,session?.status]);
 // F07Z-143: the review view model. Pure derivation from the session; this panel still owns
 // polling, submission and the pending-command state machine.
 const review=session?getInterviewReviewPresentation({session,goalTitle:goalTitle??null,hasSelectedJob}):null;
 // F07Z-67: an open session's own mode wins. Otherwise opening a 陪练 session from history
 // while the shell's last choice was 实战 would show the wrong agent on that session.
 const sessionMode:('interview'|'coaching'|null)=session?(session.policy.mode==='coaching'?'coaching':'interview'):null;
 const shownMode=sessionMode??agentMode;
 // F07Z-143: scripted vs AI is decided once in reviewPresentation (no marker means scripted).
 const endReason=report?.stop_reason?(stopReasonLabel[report.stop_reason]??report.stop_reason):'服务端未给出结束原因';
 const historyBlock=<div className="interview-history"><div className="section-heading"><h2>该目标的历史场次</h2>
   <button className="text-button" disabled={busy} onClick={()=>void loadHistory().catch(e=>setError(interviewError(e)))}>刷新历史</button></div>
   {history.length===0?<p className="muted">还没有整场面试记录。</p>:history.map((item,index)=><button className="history-item" key={item.id}
    disabled={busy} onClick={()=>{if(onOpenSession)onOpenSession(item.id);else void openSession(item.id);}}><span>场次 {history.length-index} · {item.id.slice(0,8)} · {item.question_banks.map(b=>`${b.name} v${b.version}`).join('、')||'默认题库'}</span>
    <small>{item.policy.mode==='coaching'?'陪练':'实战'} · {historyStatus(item)} · {item.events.filter(e=>e.kind==='question').length} 题 / {item.events.filter(e=>e.kind==='answer').length} 答</small></button>)}
   <p className="fine-print">刷新后可从这里打开原场次；页面只保护当前交互内尚未提交的输入，不提供长期草稿。</p></div>;

 return <section className="card interview-workspace" id="interview-workspace" ref={sectionRef} aria-label="整场面试">
  {desktop
   ?<div className="section-heading d-workspace-head"><h2 className="d-card-title">{session?(terminal?'本场复盘':'面试进行中'):'选择题库，开始整场面试'}</h2>
    {session&&<button className="text-button" disabled={busy} onClick={()=>void refresh().catch(()=>{})}>刷新状态</button>}
    {session&&!terminal&&<button className="text-button" data-testid="interview-stop" disabled={busy}
     onClick={()=>void stopSession()}>结束本场</button>}</div>
   :<><div className="section-heading"><span className="eyebrow">FULL INTERVIEW · 整场面试</span>
    {session&&<button className="text-button" disabled={busy} onClick={()=>void refresh().catch(()=>{})}>刷新状态</button>}
    {session&&!terminal&&<button className="text-button" data-testid="interview-stop" disabled={busy}
     onClick={()=>void stopSession()}>结束本场</button>}</div>
    <h1>两题整场练习</h1><p className="muted">选择一个已启用题库，按 AI 应用岗位、面试模式开一场两题练习。倒计时以服务端 deadline_at 为准。</p></>}
  {desktop&&<div className="d-agent-banner" data-testid="interview-agent-banner" data-mode={shownMode}>
    <div className="d-agent-banner-head"><span className="d-agent-badge" aria-hidden="true">AI</span>
     <strong>{shownMode==='coaching'?'面试训练 Agent':'面试官 Agent'}</strong><span className="tag">{shownMode==='coaching'?'教练与复测':'正式模拟'}</span></div>
    <p className="fine-print">{shownMode==='coaching'
     ?'教练模式会引用你的原回答讲解，并就同一主题再出一道复测题。'
     :'正式模拟不给答案、提示或讲解；本场结束后看复盘。'}</p>
   </div>}
  {routeMissing&&<div className="d-route-missing" role="status" data-testid="session-missing">
   <strong>找不到这场面试记录。</strong>
   <p>链接里的场次可能已被删除，或者不属于当前账号。</p>
   <button type="button" className="secondary" data-testid="session-missing-back"
    onClick={()=>{setRouteMissing(false);reset();}}>返回面试</button>
  </div>}
  {error&&<div className="d-error-block"><p className="error" role="alert">{error}</p>
   <OperationErrorDetails detail={errorDetail}/></div>}
  {notice&&<p className="notice interview-notice" role="status" data-testid="interview-notice">{notice}</p>}
  {!session&&<div className="interview-setup">
   {jobKeyPoints&&<p className="fine-print d-job-points" data-testid="interview-job-points">
    已选岗位的原文重点：{jobKeyPoints.title}{jobKeyPoints.skills.length?` · ${jobKeyPoints.skills.join(' · ')}`:''}（按 JD 词面匹配，只作背景参考，不等于出题范围）</p>}
   {/* F07Z-67: the mode is chosen here, once, so the side bar needs a single 面试 entry. It is
       fixed for a session (the server stores it in policy), so it only shows while nothing is open. */}
   {onAgentModeChange&&<>
    <span className="setup-label">面试模式</span>
    <div className={desktop?'d-mode-switch':'mode-switch'} role="group" aria-label="面试模式"
     data-testid="interview-mode-switch" data-mode={shownMode}>
     <button type="button" aria-pressed={shownMode==='interview'} disabled={busy}
      data-testid="mode-interview" onClick={()=>onAgentModeChange('interview')}>
      实战评估<small>不给提示，结束时看复盘</small></button>
     <button type="button" aria-pressed={shownMode==='coaching'} disabled={busy}
      data-testid="mode-coaching" onClick={()=>onAgentModeChange('coaching')}>
      陪练反馈<small>讲方法并复测，标记为已辅导</small></button>
    </div>
    <p className="fine-print">模式在开场时固定：换模式请另开一场。陪练中讲过的主题会标记为「已辅导」，
     用来和实战场次区分；产品不做评分，也不会把讲解当成你的实战表现。</p>
   </>}
   <label htmlFor="interview-bank">题库与版本</label>
   <div className="bank-row">
    <select id="interview-bank" value={bankChoice} onChange={e=>setBankChoice(e.target.value)} disabled={busy}>
     {banks.length===0&&<option value="">暂无已启用题库</option>}
     {banks.map(bank=><option key={`${bank.bank_id}|${bank.version}`} value={`${bank.bank_id}|${bank.version}`}>{bankLabel(bank)}</option>)}
    </select>
    <button type="button" className="text-button" disabled={busy} onClick={()=>void loadBanks().catch(e=>setError(interviewError(e)))}>刷新题库</button>
   </div>
   <p className="fine-print" data-testid="interview-question-source">本场题目来自上面这份题库
    {bankChoice?`（${banks.find(item=>`${item.bank_id}|${item.version}`===bankChoice)?.name??''}）`:''}；
    岗位原文重点不会用来硬筛题目，也不会替你作答。</p>
   <p className="fine-print">仅列出已启用题库；在「题库」页导入并启用后会出现在这里，也可以点「刷新题库」重新读取。
    {assessmentMode==='live'
     ?'本场会调用真实模型，按实际用量计费（费用以账本为准）。'
     :'本场使用免费 scripted 流程演示，不启用真实模型调用。'}</p>
   <label className="d-check-row interview-mistakes">
    <input type="checkbox" checked={withMistakes} disabled={busy}
     onChange={e=>setWithMistakes(e.target.checked)}/>
    <span>先问最近做错的算法题（来自「算法练习」记录）</span></label>
   <p className="fine-print">勾选后本场第一题就是你的错题，用来让你讲思路；教练模式会让你讲完后再听方法。
    一场通常只讲一道：最多带 2 条记录，但讲完第一道会接着出复述题。这些题不打分：本产品不判算法题。
    最近没有「没做出来 / 做出一部分」的记录时，勾选会被忽略。
    题目名、你的复盘和粘贴的题面会随请求发给你配置的模型（离设备）。</p>
   <button className="primary" disabled={busy||!bankChoice||goalSnapshot.status==='CANCELLED'||pendingVisible} onClick={()=>void start()}>
    {busy?getAsyncActionCopy('interview-start','submitting').buttonLabel:'开始整场面试 →'}</button>
   {goalSnapshot.status==='CANCELLED'&&<p className="error">当前求职目标已结束，请先新建或选择其他目标。</p>}
   {activeSessionId&&<button className="secondary" disabled={busy} onClick={()=>void openSession(activeSessionId)}>打开进行中的原场次</button>}
  </div>}
  {pendingVisible&&<div className="interview-retry" data-testid="unknown-retry">
   <p className="muted">结果未确认：请先刷新状态核对，确未生效时用同一 URL、同一 body、同一幂等键手动重放。页面不会自动重发 POST。</p>
   <button className="secondary" disabled={busy||replayBlocked} onClick={()=>void retry()}>用同一请求重试</button>
   {replayBlocked&&<p className="error" data-testid="replay-blocked">{replayBlockedMessage(replayGate,replayTargetId)}</p>}</div>}
  {conflict&&<p className="fine-print" data-testid="conflict-hint">这是已确认的状态变化：旧请求（含旧幂等键）已废弃，请按上方提示核对后用最新状态重新操作。</p>}
  {session&&<div className="interview-session" data-session-id={session.id}>
   {!desktop&&<div className="chips"><span className="tag">场次 {session.id.slice(0,8)}</span>
    <span className="tag" data-testid="interview-status">状态 · {statusLabel[session.status]??session.status}</span>
    {session.question_banks.map(bank=><span className="tag" key={`${bank.bank_id}|${bank.version}`}>{bank.name} · v{bank.version}</span>)}
    <span className="phase">{remaining>0&&!terminal?`剩余 ${formatRemaining(remaining)}`:'已到期/已结束'}</span></div>}
   <div className={desktop?'d-question-scroll':undefined}>
   {isProcessing(session.status)&&<p className="notice interview-notice" role="status">正在出题或处理上一答，页面会自动回读状态；离开或结束后停止。</p>}
   {expired&&!terminal&&<p className="error" data-testid="deadline-notice">本场已到服务端 deadline，已停止作答与答题重放；正在回读终态，结束原因以服务端返回为准。</p>}
   {expired&&terminal&&<p className="notice" data-testid="deadline-end">服务端已结束本场：{statusLabel[session.status]??session.status} · {endReason}</p>}
   {session.events.filter(e=>e.kind==='explanation').map(event=>{
    const payload=event.payload as ExplanationPayload;
    return <article className="interview-explanation" key={event.id} data-testid="interview-explanation">
     <span className="eyebrow">教练讲解 · {payload.origin==='real_model'?'模型输出，非标准答案':'脚本演示，不含讲解内容'}</span>
     <p>{payload.text}</p>
    </article>;
   })}
   <p className="fine-print" data-testid="interview-role-scope">
    题库与题型目前支持两个方向（Java 后端 / AI 应用）；自定义岗位会用你选的题库与对应方向出题，
    不提供该岗位的专用评分。</p>
   {session&&Boolean((session.policy as {bank_source?:unknown}).bank_source)&&
   <p className="fine-print" data-testid="interview-bank-source">
    本场可从「你的题库 {((session.policy as {bank_source?:{bank_items?:number}}).bank_source?.bank_items)??0} 题」和
    「角色基础 {((session.policy as {bank_source?:{role_pack_items?:number}}).bank_source?.role_pack_items)??0} 题」里选题；
    用哪一条由面试官判断，不会因为库里没有合适题而停止。</p>}
  {question&&!terminal&&<><span className="eyebrow">当前题 · {session.events.filter(e=>e.kind==='question').length}/{session.policy.max_questions}</span>
    <h2 className="interview-question" data-testid="interview-question">{questionText(question)}</h2>
    {(question.payload as QuestionPayload).source==='jd_generated'&&
     <p className="fine-print" data-testid="interview-generated-source">{(question.payload as QuestionPayload).generated?.target_kind==='direction'
      ?'岗位方向练习题':'按 JD 生成'} · 无已核验参考答案</p>}
    {(question.payload as QuestionPayload).wiki&&
     <p className="fine-print" data-testid="interview-wiki-source">面试 Wiki v{(question.payload as QuestionPayload).wiki?.version}</p>}</>}
   {clarification&&!terminal&&<p className="fine-print interview-clarification" data-testid="interview-clarification">
     {String((clarification.payload as {text?:string}).text??'')}</p>}
   {!question&&isProcessing(session.status)&&<p className="muted">首题尚未生成，请稍候。</p>}
   {session.status==='WAITING_CREDENTIALS'&&<p className="error">{runErrorLabel[session.run?.error_code??'']??'后台处理已暂停，请检查本机 Worker 配置。'}</p>}
   {session.status==='FAILED'&&<p className="error">本场失败：{runErrorLabel[session.run?.error_code??'']??session.run?.error_code??'后台任务未完成'}。已保存的记录仍可回读。</p>}
   {terminal&&Boolean(report?.generated_questions)&&
    <p className="fine-print" data-testid="review-generated-count">本场实际使用 {report?.generated_questions} 道按岗位生成的题。</p>}
   {terminal&&Boolean(report?.wiki_questions)&&
    <p className="fine-print" data-testid="review-wiki-count">本场从 Wiki 复用了 {report?.wiki_questions} 个提问回合。</p>}
   {terminal&&report?.jd_coverage&&report.jd_coverage.length>0&&
    <details className="d-card" data-testid="review-jd-coverage">
     <summary>岗位考点 · 问到 {report.jd_coverage.filter(item=>item.asked_count>0).length}/{report.jd_coverage.length} 项</summary>
     <ul>{report.jd_coverage.map(item=><li key={item.id}>{item.quote} · {
      item.asked_count>0?`问到 ${item.asked_count} 次`:item.planned_sources.length?'有候选，未问到':'未匹配候选'
     }</li>)}</ul>
    </details>}
   {terminal&&Boolean(report?.public_references?.length)&&
    <details className="d-card" data-testid="review-public-references">
     <summary>本场题目引用的官方资料</summary>
     {report?.public_references?.map(item=><div key={item.id+item.question_title}>
      <p>{item.question_title} · <a href={item.url} target="_blank" rel="noopener noreferrer">{item.title}</a></p>
      <blockquote>{item.quote}</blockquote>
     </div>)}
    </details>}
   {terminal&&Boolean(report?.experience_references?.length)&&
    <details className="d-card" data-testid="review-experience-references">
     <summary>本场题目参考的公开面经</summary>
     {report?.experience_references?.map(item=><div key={item.id+item.question_title}>
      <p>{item.question_title} · <a href={item.url} target="_blank" rel="noopener noreferrer">{item.title}</a></p>
      <blockquote>{item.quote}</blockquote>
     </div>)}
    </details>}
   {terminal&&review&&<InterviewReview review={review} busy={busy} desktop={desktop}
    onOpenApplications={desktop?onOpenApplications:undefined} onStartAnother={startAnother}
    onStartRetest={startFocus}
    onRefresh={()=>void refresh().catch(()=>{})}/>}
   </div>
   {session.status==='WAITING_FOR_USER'&&question&&<form className="interview-answer" onSubmit={e=>void submit(e)}>
    <label htmlFor="interview-answer">你的整场面试回答</label>
    {remind&&!terminal&&<p className="fine-print" data-testid="answer-time-hint">还有时间：可以继续补充、提交现有思路，或点「跳过本题」。本题的建议时间已到，整场仍按服务端截止时间接收答案。</p>}
    <textarea id="interview-answer" value={text} rows={9} maxLength={MAX_ANSWER_LENGTH} disabled={busy||expired}
     placeholder="写下你的判断、依据，以及会怎样处理…"
     onChange={e=>{setText(e.target.value);writeDraft(userId,session.id,question.id,e.target.value);}}/>
    <div className="answer-toolbar"><span className="muted">{text.length}/{MAX_ANSWER_LENGTH} · 非空{!text.trim()?' · 请输入回答':''}</span>
     <button className="text-button" type="button" data-testid="answer-clarify"
      disabled={busy||expired||pendingVisible} onClick={()=>void clarifyQuestion()}>澄清题意</button>
     <button className="secondary" type="button" data-testid="answer-skip"
      disabled={busy||expired||pendingVisible} onClick={()=>void skipQuestion()}>跳过本题</button>
     <button className="primary" type="submit" data-testid="answer-submit"
      disabled={busy||!text.trim()||text.length>MAX_ANSWER_LENGTH||expired||pendingVisible||savedAnswer}>
      {busy?getAsyncActionCopy('answer-submit','submitting').buttonLabel:'提交这一答 →'}</button></div>
    {savedAnswer&&<p className="error" data-testid="saved-answer-hint">该题已有已保存回答，请刷新状态核对，不要再次提交。</p>}
    {expired&&<p className="error">已到服务端 deadline，提交已停用；输入仍保留。</p>}
   </form>}
  </div>}
  {(!desktop||!session||session.status!=='WAITING_FOR_USER')&&historyBlock}
 </section>;
}
