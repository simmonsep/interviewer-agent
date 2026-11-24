// Public projection of the whole-session interview API.
// Backend contracts (backend/src/interview_agent/interviews/) remain authoritative.

export interface BankSummary {
  bank_id:string;version:number;origin:'builtin'|'private'|string;enabled:boolean;revision:number;
  content_sha256:string;name:string;item_count:number;
}

export interface QuestionPayload {
  topic_id:string;question_id:string;text:string;kind:string;pack_version:string;assisted:boolean;
  has_reference?:boolean;bank_item?:Record<string,unknown>;parent_question_id?:string|null;
  origin?:string;practice_record_id?:string;
  // F07Z-159/162: which source the question came from, and the frozen strategy id if one was used.
  source?:'role_pack'|'builtin_bank'|'private_bank'|'project_material'|string;
  generated?:{target_kind:'direction'|'user_jd'|'selected_job'};
  wiki?:{id:string;version:number;topic_key:string};
  strategy_id?:string;
  fallback_reason?:string;
}
export interface SessionSkill {
  id:string;version:number;name:string;origin:'preinstalled'|'user_install'|string;author?:string|null;
}
export interface SessionStrategy {id:string;title:string}
export interface AnswerPayload {
  text:string;question_id:string;topic_id:string;assisted:boolean;
}
export interface ExplanationPayload {
  topic_id:string;text:string;origin?:string;evidence_id?:string;evidence_quote?:string;
}
export interface InterviewEvent {
  id:string;seq:number;kind:'question'|'answer'|'explanation'|'skip'|'clarification';
  payload:QuestionPayload|AnswerPayload|Record<string,unknown>;
}

export interface Observation {
  id:string;answer_id:string;topic_id?:string;label:string;summary:string;answer_quote:string;
  evidence_id:string;evidence_quote:string;origin?:string;verification?:string;
  evidence_source?:{id:string;url:string;origin:string;version:string};
  correction?:string|null;
}
export interface InterviewReport {
  stop_reason:string|null;model_summary:string|null;questions:number;answers:number;
  assisted_answers:number;observations:Observation[];semantic_scoring_eligible:boolean;
  unreviewed_answer_ids:string[];next_actions:string[];summary_origin?:string;
  generated_questions?:number;
  wiki_questions?:number;
  jd_coverage?:{id:string;quote:string;planned_sources:string[];asked_count:number}[];
  public_references?:{id:string;title:string;url:string;quote:string;question_title:string;
    fetched_at:string;content_sha256:string}[];
  experience_references?:{id:string;title:string;url:string;quote:string;question_title:string;
    fetched_at:string;content_sha256:string}[];
  // 新增业务 1: how many questions actually came from the selected bank.
  bank_usage?:{bank_questions:number;total_questions:number;
    declaration?:'used'|'not_relevant'|'unknown'|null};
  // 收尾 01: server-resolved drill for an observation (authored retest prompt, frozen pack
  // version). Absent when the observation has no reference or the topic has no retest.
  retest?:RetestSuggestion[];
}
export interface RetestSuggestion {
  observation_id:string;answer_id:string;topic_id:string;topic_title:string;
  question:string;reference_url:string;pack_version:string;assisted:boolean;reason:string;
}
export interface InterviewRun {
  id:string;status:string;error_code:string|null;model_requests:number;origin:string|null;
  tool_log:Record<string,unknown>[];diagnostic:Record<string,unknown>|null;
}
export interface InterviewSession {
  id:string;version:number;revision:number;status:string;goal_version:number;
  policy:{role:string;mode:string;max_questions:number;duration_seconds:number;
    minimum_questions?:number;allow_project_questions?:boolean;
    // F07Z-65: persisted at creation, so a paused session can restore the checkbox even when
    // no question event exists yet (the first question may never have been generated).
    include_practice_mistakes?:boolean;practice_mistake_count?:number};
  pack_version:string;deadline_at:string;question_banks:BankSummary[];
  skills?:SessionSkill[];
  strategies?:SessionStrategy[];
  events:InterviewEvent[];observations:Observation[];report:InterviewReport|null;
  run:InterviewRun|null;
}

export const ACTIVE_STATUSES=['PENDING','RUNNING','WAITING_FOR_USER','WAITING_CREDENTIALS'];
export const isTerminal=(status:string)=>!ACTIVE_STATUSES.includes(status);
export const isProcessing=(status:string)=>status==='PENDING'||status==='RUNNING';

export const statusLabel:Record<string,string>={
  PENDING:'正在出题',RUNNING:'正在处理上一答',WAITING_FOR_USER:'可作答',
  WAITING_CREDENTIALS:'后台已暂停',COMPLETED:'已结束',FAILED:'失败',
  CANCELLED:'已结束',STALE:'已结束（目标或材料已变化）'
};

export const stopReasonLabel:Record<string,string>={
  question_limit:'题数已达到本场上限',agent_finished:'面试 Agent 结束本场',
  deadline:'本场已到期',user_stop:'已提前结束',context_changed:'目标或材料已变化，本场已结束',
  wiki_source_withdrawn:'本场使用的 Wiki 版本已撤回'
};

export const runErrorLabel:Record<string,string>={
  model_not_enabled:'后台评价器未启用，请启用本机 scripted 面试 Worker。',
  model_credentials_unavailable:'本机缺少模型凭证，本场已暂停。',
  model_authorization_unavailable:'模型调用授权未就绪，本场已暂停。',
  model_budget_exhausted:'调用额度已用尽，已停止新请求。',
  model_usage_unknown:'上次调用费用不明确，已停止新请求。',
  attempt_limit:'后台任务重试次数已达上限。',
  interview_failed:'后台面试任务失败。'
};

export function latestQuestion(session:InterviewSession|null):InterviewEvent|null {
  if(!session)return null;
  for(let i=session.events.length-1;i>=0;i--)if(session.events[i].kind==='question')return session.events[i];
  return null;
}
export function questionText(event:InterviewEvent|null):string {
  return (event?.payload as QuestionPayload|undefined)?.text??'';
}
export function answerCount(session:InterviewSession|null):number {
  return session?.events.filter(e=>e.kind==='answer').length??0;
}
export function formatRemaining(ms:number):string {
  const total=Math.max(0,Math.floor(ms/1000));
  const h=Math.floor(total/3600),m=Math.floor((total%3600)/60),s=total%60;
  const pad=(n:number)=>String(n).padStart(2,'0');
  return h>0?`${h}:${pad(m)}:${pad(s)}`:`${pad(m)}:${pad(s)}`;
}
