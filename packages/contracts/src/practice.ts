export interface PracticeQuestion {id:string;text:string;topic:string;version:string;review:string;semantic_scoring_eligible:false}
export interface PracticeFeedback {
 summary:string;feedback:string;semantic_scoring_eligible:false;origin:string;provider:string|null;
 citations:{kind:'answer'|'evidence';source_id:string;version:string;start_line:number;end_line:number;quote:string;source:{title:string;url:string;locator:string;summary_kind:string}|null}[];
}
export interface PracticeSessionView {
 id:string;version:number;status:string;mode:string;context_mode:string;target_id:string;question:PracticeQuestion;created_at:string;
 answer:{id:string;text:string;origin:string}|null;
 operation:{id:string;status:string;error_code:string|null}|null;
 feedback:PracticeFeedback|null;
}
