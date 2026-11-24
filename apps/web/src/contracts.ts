// Public Python DTO projection. Backend Pydantic validation is authoritative.
export interface PracticeQuestion {
 id:string; text:string; topic:string; version:string;
 review:'source_checked_draft'; semantic_scoring_eligible:false;
}
export interface PracticeFeedback {
 summary:string; feedback:string; semantic_scoring_eligible:false; origin:string; provider:string|null;
 citations:{source_id:string;version:string;kind:'answer'|'evidence';start_line:number;end_line:number;quote:string;
  source:{title:string;url:string;locator:string;summary_kind:'authored_paraphrase'}|null}[];
}
export interface PracticeSessionView {
 id:string;version:number;status:'WAITING_FOR_ANSWER'|'EVALUATING'|'COMPLETED'|'CANCELLED'|'FAILED';
 mode:'practice';context_mode:'role_only';target_id:string;question:PracticeQuestion;created_at:string;
 answer:{id:string;text:string;origin:'user_submitted'}|null;
 operation:{id:string;status:'PENDING'|'RUNNING'|'SUCCEEDED'|'FAILED'|'CANCELLED'|'WAITING_CREDENTIALS';error_code:string|null}|null;
 feedback:PracticeFeedback|null;
}
