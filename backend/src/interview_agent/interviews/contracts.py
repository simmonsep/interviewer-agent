from typing import Annotated,Literal
from pydantic import Field,field_validator,model_validator
from interview_agent.contracts.common import Contract,PositiveInt
from interview_agent.contracts.practice import UuidString
from .catalog import topic
from .simulation import is_closing
from interview_agent.question_banks.contracts import BankRef

PROJECT_SUMMARY_VERSION='project-extractive-v1'


def project_summary_enabled(scope):
    return scope['policy'].get('feedback_summary',{}).get('version')==PROJECT_SUMMARY_VERSION


def project_answer_summary(text,label):
    excerpt=text[:120]+('…' if len(text)>120 else '')
    tail='建议补充具体证据继续核验。' if label=='needs_probe' else ''
    return '个人经历未核验。原回答节选：「'+excerpt+'」。'+tail


def session_summary(scope):
    questions=scope['question_history'];projects=sum(q['topic_id']=='project' for q in questions)
    practice=sum(q.get('origin')=='practice_record' for q in questions)
    technical=len(questions)-projects-practice
    text=f'本轮结束，已出题{len(questions)}道（项目{projects}道、技术{technical}道'
    text+=f'、练习错题{practice}道）' if practice else '）'
    text+='。个人经历未核验，项目回答见原文节选；技术反馈是基于引用的模型观察，仍需复核。'
    if practice:text+='练习错题来自你的手动记录，没有参考，只保留回答、不作正确性判断。'
    if any(q.get('has_reference') is False for q in questions):text+='未提供参考的题只记录回答，不作正确性判断。'
    return text


class CreateInterview(Contract):
    resume_item_indexes:Annotated[list[Annotated[int,Field(ge=0,le=7)]],Field(max_length=6)]=[]
    question_banks:Annotated[list[BankRef],Field(max_length=2)]=[]
    allow_project_questions:bool|None=None
    alignment_run_id:UuidString|None=None
    expected_version:PositiveInt
    # F07Z-166: omit to let the server resolve the role from the goal's confirmed context.
    role:Literal['java-backend','ai-application']|None=None
    mode:Literal['interview','coaching']='interview'
    # F07Z-167: 'full' freezes the 45-minute, 10-12 round simulation; the server owns the
    # numbers. Omitting it (old clients) or 'quick' keeps the caller's values.
    profile:Literal['quick','full']|None=None
    max_questions:Annotated[int,Field(ge=1,le=12)]=4
    duration_seconds:Annotated[int,Field(ge=60,le=3600)]=1200
    fact_ids:Annotated[list[UuidString],Field(max_length=6)]=[]
    draft_id:UuidString|None=None
    # F07Z-166: an explicit "no resume" must not be turned back into the bound resume here.
    use_resume:bool=True
    application_id:UuidString|None=None
    application_package_revision:Annotated[int,Field(ge=1,le=30)]|None=None
    interview_round:Annotated[str,Field(max_length=80)]=''
    # Pull the latest unsolved/partial practice records into this session as topics that are
    # asked but never scored (P3.3b). Off by default so an ordinary session is unchanged.
    include_practice_mistakes:bool=False
    # F07Z-165 A: start a coaching session with the review's retest suggestion as its first
    # question. The server re-checks the source session/observation and freezes the question.
    focus_session_id:UuidString|None=None
    focus_observation_id:UuidString|None=None

    @model_validator(mode='after')
    def application_scope(self):
        if len(set(self.resume_item_indexes))!=len(self.resume_item_indexes):raise ValueError('duplicate_resume_item')
        if self.resume_item_indexes and (not (self.draft_id or self.application_id) or self.fact_ids):raise ValueError('resume_selection_requires_draft_or_application')
        if len({r.bank_id for r in self.question_banks})!=len(self.question_banks):raise ValueError('duplicate_bank_selection')
        if not self.application_id and (self.application_package_revision is not None or self.interview_round):
            raise ValueError('application_required')
        if self.application_id and self.draft_id:
            raise ValueError('choose_application_or_draft')
        if self.interview_round and (not self.interview_round.strip() or '\x00' in self.interview_round):
            raise ValueError('invalid_interview_round')
        if bool(self.focus_session_id)!=bool(self.focus_observation_id):
            raise ValueError('focus_requires_session_and_observation')
        if not self.use_resume and (self.draft_id or self.application_id or self.fact_ids
                                    or self.resume_item_indexes):
            raise ValueError('resume_excluded')
        if self.focus_session_id:
            if self.mode!='coaching':raise ValueError('focus_requires_coaching')
            if self.include_practice_mistakes:raise ValueError('focus_excludes_practice_mistakes')
            if self.application_id or self.draft_id:raise ValueError('focus_excludes_application_or_draft')
            if self.fact_ids or self.resume_item_indexes:raise ValueError('focus_excludes_material_selection')
        if self.profile=='full' and self.mode!='interview':raise ValueError('full_requires_interview')
        return self

    @field_validator('fact_ids')
    @classmethod
    def unique(cls,v):
        if len(v)!=len(set(v)):raise ValueError('duplicate_fact')
        return v

    @field_validator('alignment_run_id')
    @classmethod
    def normalized_alignment(cls,value):
        from uuid import UUID
        return str(UUID(value)) if value else None


class Answer(Contract):
    expected_version:PositiveInt
    question_id:UuidString
    text:Annotated[str,Field(min_length=1,max_length=1500)]

    @field_validator('text')
    @classmethod
    def nonblank(cls,v):
        if not v.strip() or '\x00' in v:raise ValueError('answer_required')
        return v


class SkipQuestion(Contract):
    """F07Z-167: skip the current question. The round still counts as asked; no observation is
    produced, and the skipped question can never be scored as wrong."""
    expected_version:PositiveInt
    question_id:UuidString


class ClarifyQuestion(Contract):
    """F07Z-167: ask what the current question covers. The server restates the frozen scope only;
    the question id, round position and clock are unchanged and no model call is made."""
    expected_version:PositiveInt
    question_id:UuidString


class Stop(Contract):
    expected_version:PositiveInt


class Correction(Contract):
    reason:Annotated[str,Field(min_length=1,max_length=300)]

    @field_validator('reason')
    @classmethod
    def nonblank(cls,v):
        if not v.strip():raise ValueError('reason_required')
        return v


class Observation(Contract):
    answer_id:UuidString
    label:Literal['needs_probe','supported_in_this_answer','inconclusive']
    summary:Annotated[str,Field(min_length=1,max_length=200,description='技术题写有引用的简短诊断；项目题开启feedback_summary策略时，服务端从当前完整回答生成逐字节选。个人经历摘要不补写时序、结果或数字。')]
    answer_quote:Annotated[str,Field(min_length=1,max_length=300)]
    evidence_id:Annotated[str,Field(min_length=1,max_length=80)]
    evidence_quote:Annotated[str,Field(min_length=1,max_length=300)]


class Decision(Contract):
    action:Literal['ask_question','teach_and_retest','finish']
    topic_id:Annotated[str,Field(max_length=40)]=''
    question_id:Annotated[str,Field(max_length=80)]=''
    question_text:Annotated[str,Field(max_length=400)]=''
    fact_id:UuidString|None=None
    project_focus:Literal['contribution','verification','failure','tradeoff']='contribution'
    observation:Observation|None=None
    explanation:Annotated[str,Field(max_length=600)]=''
    summary:Annotated[str,Field(max_length=400)]=''
    # 新增业务 1: the interviewer says whether the selected bank was relevant to this JD. It is a
    # declaration about its own choice, not a scoring field; 'unknown' keeps older callers valid.
    bank_use:Literal['used','not_relevant','unknown']='unknown'
    # 新增业务 1 · B: optional project follow-up strategy (how to probe, never a question)
    strategy_id:Annotated[str,Field(max_length=60)]=''
    # 新增业务 1 §2.1: when a matching private candidate exists but the model asks a
    # role-pack question, it must say why and name the candidate it is passing over.
    fallback_reason:Annotated[str,Field(max_length=160)]=''
    fallback_candidate_id:Annotated[str,Field(max_length=60)]=''
    tool_log:Annotated[list[dict],Field(max_length=8)]=[]


def first_practice_topic(scope):
    """The practice-record topic the session must open with, if the user included one."""
    for identity in scope['pack']['topic_order']:
        if scope['pack']['topics'].get(identity,{}).get('origin')=='practice_record':
            return identity
    return None


def question_coverage(scope):
    """Only newly frozen mixed-interview policies require both question kinds."""
    if scope['policy'].get('coverage',{}).get('version')!='project-technical-v1':return None
    asked={'project' if q['topic_id']=='project' else 'technical' for q in scope['question_history']}
    return {'required':['project','technical'],'missing':[k for k in ('project','technical') if k not in asked],
            'remaining_questions':scope['policy']['max_questions']-len(scope['question_history'])}


def validate_decision(scope,value):
    d=Decision.model_validate(value)
    answer=scope['answer'];current=scope['current_question'];past=scope['question_history']
    focus=scope.get('focus')
    if focus and not answer and not past:
        # F07Z-165 A: the first question of a targeted coaching session is frozen by the server.
        # It is the only allowed decision until the user answers; everything else stays guarded.
        if scope['policy']['mode']!='coaching':raise ValueError('focus_requires_coaching')
        if d.action!='ask_question' or d.topic_id!=focus['topic_id'] or d.question_id!=focus['question_id']:
            raise ValueError('focus_first_question_required')
        if d.question_text or d.fact_id or d.strategy_id or d.observation or d.summary or d.explanation:
            raise ValueError('invalid_focus_decision')
        return d
    if answer:
        o=d.observation
        if o is None or o.answer_id!=answer['id'] or not o.answer_quote.strip() or o.answer_quote not in answer['text']:raise ValueError('answer_citation_required')
        refs=topic(scope,current['topic_id'])['references']
        if current['topic_id']=='project':refs=[r for r in refs if r['id']==current['fact_id']]
        if not any(r['id']==o.evidence_id and o.evidence_quote.strip() and o.evidence_quote in r['text'] for r in refs):raise ValueError('reference_citation_required')
        if not o.summary.strip():raise ValueError('observation_required')
        if current['topic_id']=='project' and project_summary_enabled(scope):
            o.summary=project_answer_summary(answer['text'],o.label)
        current_topic=topic(scope,current['topic_id']) if current['topic_id']!='project' else None
        no_reference=bool(current_topic) and not current_topic.get('has_reference',True)
        if no_reference:
            # A+ contract: `inconclusive` is a deterministic product state, not a model choice.
            # Practice-record topics carry no reference answer at all, so coaching may still
            # walk through an approach (the label stays inconclusive), while a topic that
            # merely lost its reference is refused teaching entirely.
            if d.action=='teach_and_retest' and current_topic.get('assessment')!='none':
                raise ValueError('question_has_no_reference_for_assessment')
            o.label='inconclusive'
            o.summary=('本题来自你的练习记录，没有参考；只保留回答，不作正确性判断。'
                       if current_topic.get('assessment')=='none'
                       else '本题未提供参考；仅保存原回答，不作正确性判断。')
            if d.action=='finish':d.summary='本轮已结束；无参考题只保留回答，不作正确性判断。'
        elif o.label=='inconclusive':
            # A+ contract: with a usable reference, "I cannot judge" is not a product state;
            # the safe business reading is "evidence is insufficient, keep probing".
            o.label='needs_probe'
    elif d.observation is not None:raise ValueError('answer_missing')
    if d.action=='finish':
        if not past or d.topic_id or d.question_id or d.question_text or d.fact_id or d.explanation:raise ValueError('invalid_finish')
        closing=is_closing(scope['policy'],scope.get('remaining_seconds'))
        if not closing and len(past)<scope['policy'].get('minimum_questions',min(2,scope['policy']['max_questions'])):
            raise ValueError('minimum_coverage_not_met')
        coverage=question_coverage(scope)
        if coverage and coverage['missing']:raise ValueError('question_coverage_required')
        if project_summary_enabled(scope):d.summary=session_summary(scope)
        return d
    if len(past)>=scope['policy']['max_questions']:raise ValueError('question_limit_finish_required')
    if d.summary:raise ValueError('summary_only_at_finish')
    coverage=question_coverage(scope)
    if coverage and coverage['remaining_questions']<=len(coverage['missing']):
        if ('project' if d.topic_id=='project' else 'technical') not in coverage['missing']:
            raise ValueError('question_coverage_required')
    # F07Z-59: checking "include practice mistakes" promises the user that their own mistake
    # is actually asked. Ordering the pack is not enough, because the live model chooses.
    # This only constrains the FIRST question of the session.
    if not past:
        practice=first_practice_topic(scope)
        if practice and d.topic_id!=practice:raise ValueError('practice_mistake_first')
    if d.strategy_id:
        from .project_strategy import anchors_are_grounded
        frozen = {card['id']: card for card in (scope.get('strategies') or [])}
        if d.strategy_id not in frozen:
            raise ValueError('strategy_not_frozen')
        if d.topic_id != 'project':
            raise ValueError('strategy_only_for_project_questions')
        if not scope['policy'].get('allow_project_questions', True):
            raise ValueError('strategy_requires_project_questions')
        if not anchors_are_grounded(frozen[d.strategy_id], fact_ids=[d.fact_id] if d.fact_id else [],
                                    answer_text=(scope.get('answer') or {}).get('text', '')):
            raise ValueError('strategy_requires_anchor')
    if d.topic_id=='project':
        if not scope['facts']:raise ValueError('project_question_not_enabled')
        if not scope['policy'].get('allow_project_questions',True):raise ValueError('project_question_not_enabled')
        # F07Z-97: `question_id` is a constant for project questions - the wording is generated
        # from the chosen fact and focus. A model that omits or mislabels that constant has still
        # made a valid choice, so normalise it instead of spending its retry on a formality.
        if d.action=='ask_question' and not d.question_text and not d.explanation:
            d.question_id='project-contribution'
        if d.question_id!='project-contribution' or d.action!='ask_question' or d.question_text or d.explanation:raise ValueError('invalid_project_question')
        if d.fact_id not in {f['id'] for f in scope['facts']}:raise ValueError('fact_not_in_scope')
        if any(q.get('fact_id')==d.fact_id and q.get('project_focus')==d.project_focus for q in past):raise ValueError('project_focus_already_asked')
        return d
    # Declaring the bank irrelevant while asking one of its questions is a contradiction; the
    # reverse (using the bank without saying so) is tolerated, because 'unknown' is the default.
    if d.bank_use=='not_relevant' and str(d.topic_id).startswith('b-'):
        raise ValueError('bank_declared_not_relevant_but_used')
    item=topic(scope,d.topic_id);q=next((x for x in item['questions'] if x['id']==d.question_id),None)
    if (item.get('bank_item') or item.get('generated')) and d.question_text:raise ValueError('bank_question_text_is_frozen')
    if q is None or d.fact_id is not None:raise ValueError('question_not_in_pack')
    first_technical = q['kind']=='base' and not any(p['topic_id']!='project' for p in past)
    # An enabled practice mistake already owns the opening question (rule above), so it must not be
    # blocked by this preference; every other rule (retest, probe, old sessions without a JD) is
    # handled by the checks that follow.
    if first_technical and not (not past and first_practice_topic(scope)==d.topic_id):
        from ..question_banks.ranking import preferred_private_ids
        context=scope.get('training_context') or {}
        preferred=preferred_private_ids(scope['pack']['topics'],jd_text=context.get('jd_text',''),
                                        direction=context.get('direction',''))
        if preferred and d.topic_id not in preferred:
            if not d.fallback_reason.strip() or d.fallback_candidate_id not in preferred:
                raise ValueError('private_candidate_preferred')
        generated = [key for key, row in scope['pack']['topics'].items() if row.get('generated')]
        if not preferred and generated and d.topic_id not in generated:
            raise ValueError('generated_question_preferred')
    if any(p['question_id']==q['id'] for p in past):raise ValueError('question_already_asked')
    if q['kind']=='probe' and (not answer or current['topic_id']!=d.topic_id):raise ValueError('followup_requires_same_topic_answer')
    if q['kind']=='retest' and not any(p['topic_id']==d.topic_id for p in past):raise ValueError('retest_requires_prior_topic')
    if d.action=='teach_and_retest':
        if scope['policy']['mode']!='coaching' or not answer or q['kind']!='retest' or current['topic_id']!=d.topic_id or not d.explanation.strip():raise ValueError('coaching_policy_violation')
    elif d.explanation:raise ValueError('explanation_not_allowed')
    # F07Z-59: in coaching, a practice mistake is answered precisely so the coach explains it.
    # Without this the live model may simply move to another topic, and the checkbox would only
    # promise that the question was asked, not that anything was taught.
    if (answer and scope['policy']['mode']=='coaching' and current['topic_id']!='project'
            and topic(scope,current['topic_id']).get('assessment')=='none'
            and len(past)<scope['policy']['max_questions'] and d.action!='teach_and_retest'):
        raise ValueError('practice_mistake_needs_explanation')
    if d.question_text and (not d.question_text.strip() or any(d.question_text.strip()==p['text'].strip() for p in past)):raise ValueError('new_question_required')
    return d


def question_payload(scope,d):
    if d.topic_id=='project':
        f=next(f for f in scope['facts'] if f['id']==d.fact_id)
        focuses={'contribution':'请区分你亲自完成的部分与团队其他人的工作。','verification':'你如何验证这个结果？有哪些能复现的证据与限制？','failure':'请描述一次失败或边界情况，以及你如何定位。','tradeoff':'为什么选择这个方案？替代方案和取舍是什么？'}
        if f.get('resume_source'):
            suffix='请说明这部分的模块职责、关键实现，以及你作出的技术选择。' if d.project_focus=='contribution' else focuses[d.project_focus]
            text='你选用的简历中写到「'+f['quote']+'」。'+suffix
        else:text='材料中你确认了「'+f['quote']+'」。'+focuses[d.project_focus]
        kind='project'
    else:
        q=next(q for q in topic(scope,d.topic_id)['questions'] if q['id']==d.question_id)
        text=d.question_text or q['text'];kind=q['kind']
    assisted=d.action=='teach_and_retest' or d.topic_id in scope['assisted_topics']
    payload={'topic_id':d.topic_id,'question_id':d.question_id,'text':text,'kind':kind,'fact_id':d.fact_id,'project_focus':d.project_focus if kind=='project' else None,
            'parent_question_id':scope['current_question']['id'] if scope['current_question'] and kind in ('probe','retest') else None,
            'assisted':assisted,'pack_version':scope['pack']['version']}
    if d.strategy_id:payload['strategy_id']=d.strategy_id
    # Project questions come from the frozen material, not from a question source; saying
    # 'role_pack' here made the review call a project question "角色基础题" (seen live in rag14).
    if kind=='project':payload['source']='project_material'
    else:
        item_origin=str(((topic(scope,d.topic_id).get('bank_item') or {}).get('origin') or 'role_pack'))
        payload['source']={'role_pack':'role_pack','builtin':'builtin_bank','private':'private_bank'}.get(item_origin,'role_pack')
    focus=scope.get('focus')
    if focus and d.question_id==focus['question_id'] and not scope['question_history']:
        # Provenance for the focused first question: where the suggestion came from, frozen at
        # creation. The text itself is the authored retest, not a freshly generated question.
        payload['source']='retest_suggestion'
        payload['origin']='retest_suggestion'
        payload['focus']={'source_session_id':focus['source_session_id'],
                          'observation_id':focus['observation_id'],
                          'source_answer_id':focus['source_answer_id'],
                          'source_pack_version':focus['source_pack_version']}
    if d.fallback_reason:payload['fallback_reason']=d.fallback_reason
    if kind=='project' and f.get('resume_source'):
        payload.update(material_origin='user_selected_resume_statement',resume_source=f['resume_source'])
    if d.topic_id!='project' and topic(scope,d.topic_id).get('bank_item'):
        payload['bank_item']=topic(scope,d.topic_id)['bank_item']
        payload['has_reference']=topic(scope,d.topic_id)['has_reference']
    prepared = scope['pack']['topics'].get(d.topic_id, {})
    if generated := prepared.get('generated'):
        from .generated_questions import GENERATED_SOURCE, WIKI_SOURCE
        wiki = prepared.get('wiki')
        payload.update(source=WIKI_SOURCE if wiki else GENERATED_SOURCE,
                       has_reference=False, generated=generated)
        if wiki:
            payload['wiki'] = wiki
    practice=prepared.get('practice')
    if practice:
        # Kept on the question payload so the report and the UI can say where the question
        # came from, and so "no reference" survives into the stored record.
        payload['has_reference']=False
        payload['origin']='practice_record'
        payload['practice_record_id']=practice['record_id']
    return payload
