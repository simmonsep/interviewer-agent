import {readFileSync} from "node:fs";
import {join} from "node:path";
import {Value} from "typebox/value";
import {AssessmentSchema,SqlSnapshotSchema,type AssessmentCandidate,type RunInput,type SqlSnapshot} from "@pi-interview/contracts";
import {ScopedAssessmentStore,type ReadWindow} from "@pi-interview/runtime-pi/store";

export interface QuestionSnapshot {
 id:string;text:string;topic:string;version:string;rubric:string[];
 review:"source_checked_draft";semantic_scoring_eligible:false;
 sources:{id:string;version:string;title:string;url:string;locator:string;lines:string[]}[];
}
export interface AssessmentContext {
 userId:string;sessionId:string;sessionVersion:number;operationId:string;answerId:string;answerText:string;question:QuestionSnapshot;
}
export interface AssessmentOutput {candidate:AssessmentCandidate;metadata:Record<string,unknown>}
export class AssessmentUnavailable extends Error {
 code:string;
 constructor(code:string){super(code);this.code=code;}
}
export function loadPracticeQuestions(root:string):QuestionSnapshot[]{
 const questions=JSON.parse(readFileSync(join(root,"fixtures/pm0-05/questions.json"),"utf8")) as {id:string;prompt:string;family_id:string;content_version:string;split:string;role_id:string;rubric:{criterion:string}[];source_ids:string[];review:{status:string;eligible_for_semantic_scoring:boolean}}[];
 const sources=JSON.parse(readFileSync(join(root,"fixtures/pm0-05/sources.json"),"utf8")) as {id:string;version:string;title:string;url:string;locator:string;summary_lines:string[]}[];
 return questions.filter(q=>q.split==='development'&&q.role_id==='agent-intern').map(q=>{
  if(q.review.status!=='source_checked_draft'||q.review.eligible_for_semantic_scoring!==false)throw Error('practice_catalog_review_changed');
  return {id:q.id,text:q.prompt,topic:q.family_id,version:q.content_version,rubric:q.rubric.map(r=>r.criterion),review:'source_checked_draft',semantic_scoring_eligible:false,
   sources:q.source_ids.map(id=>{const source=sources.find(s=>s.id===id);if(!source||!source.url.startsWith('https://'))throw Error('practice_source_missing');return {id:source.id,version:source.version,title:source.title,url:source.url,locator:source.locator,lines:source.summary_lines};})};
 });
}
export function publicQuestion(q:QuestionSnapshot){return {id:q.id,text:q.text,topic:q.topic,version:q.version,review:q.review,semantic_scoring_eligible:false as const};}
export function buildSqlAssessment(context:AssessmentContext,profile:RunInput['model_profile']){
 const q=context.question,sources=q.sources.map(s=>({source_id:s.id,version:s.version}));
 const input:RunInput={server_bound_user:context.userId,session_id:context.sessionId,session_version:context.sessionVersion,operation_id:context.operationId,parent_run_id:null,mode:'practice',context_mode:'role_only',role:'assessment',target_role:'agent-intern',question_id:q.id,context_refs:sources,allowed_tools:['read_question','read_evidence','read_current_answer','submit_assessment'],model_profile:profile,budget:{provider_requests:6,tool_calls:12,turns:6,wall_ms:120000}};
 const snapshot:SqlSnapshot={schema_version:'sql-snapshot-v1',origin:'sql_snapshot',sessions:[{id:context.sessionId,user_id:context.userId,version:context.sessionVersion,question_id:q.id,answer_id:context.answerId}],questions:[{id:q.id,target_role:'agent-intern',text:q.text,rubric:q.rubric,sources}],
  evidence:q.sources.map(s=>({source_id:s.id,version:s.version,owner_user_id:null,lines:s.lines,origin:'authored_paraphrase'})),
  answers:[{id:context.answerId,version:'v1',user_id:context.userId,session_id:context.sessionId,question_id:q.id,lines:context.answerText.split(/\r?\n/),hints_used:0,origin:'user_submitted'}]};
 if(!Value.Check(SqlSnapshotSchema,snapshot))throw Error('invalid_sql_assessment_snapshot');
 return {input,snapshot};
}
export function gateAssessment(context:AssessmentContext,candidate:unknown):AssessmentCandidate {
 if(!Value.Check(AssessmentSchema,candidate))throw Error('invalid_assessment_candidate');
 const {input,snapshot}=buildSqlAssessment(context,'pm003-scripted');
 if(candidate.answer_id!==context.answerId||candidate.question_id!==context.question.id||!candidate.citations.some(c=>c.kind==='answer')||!candidate.citations.some(c=>c.kind==='evidence'))throw Error('invalid_assessment_candidate');
 const store=new ScopedAssessmentStore(snapshot),answer=store.answerWindow(input)!;
 const windows:ReadWindow[]=[answer,...context.question.sources.map(s=>store.evidence(input,s.id,s.version,1,s.lines.length))];
 for(const citation of candidate.citations)store.verifyCitation(input,citation,windows);
 return structuredClone(candidate);
}
export function feedbackView(context:AssessmentContext,candidate:AssessmentCandidate){
 return {summary:candidate.summary,feedback:candidate.feedback,semantic_scoring_eligible:false as const,
  citations:candidate.citations.map(c=>({...c,source:c.kind==='evidence'?context.question.sources.find(s=>s.id===c.source_id&&s.version===c.version):null})).map(({source,...c})=>({...c,source:source?{title:source.title,url:source.url,locator:source.locator,summary_kind:'authored_paraphrase'}:null}))};
}
