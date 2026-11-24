"""Goal inputs, grounded extraction candidates and bounded coordinator tools."""
from typing import Annotated, Literal
from pydantic import Field, field_validator
from interview_agent.contracts.common import Contract, PositiveInt
from interview_agent.contracts.practice import UuidString

Short = Annotated[str, Field(min_length=1, max_length=200)]
Intent = Literal['auto','research','materials','training','review','finish']
# F07Z-166: why this turn exists. Only an explicit training turn may start a preparation
# automatically; a JD save and a completion notification never do.
Request = Literal['auto','training','save_jd']


def request_body(value):
    body=value.model_dump(mode='json')
    if body.get('intent') in (None,'auto'):body.pop('intent',None)
    if body.get('request') in (None,'auto'):body.pop('request',None)
    if body.get('use_resume') is True:body.pop('use_resume',None)
    if body.get('continue_after_research') is False:body.pop('continue_after_research')
    return body


class Conditions(Contract):
    direction: Annotated[str, Field(max_length=80)] = ''
    cities: Annotated[list[Annotated[str, Field(min_length=1, max_length=30)]], Field(max_length=5)] = []
    city_policy: Literal['unknown','preferred','required'] = 'unknown'
    auto_research: bool = False
    recruitment: Literal['unknown','intern','campus','experienced'] = 'unknown'
    graduation_year: Annotated[int, Field(ge=2000,le=2100)] | None = None
    days_per_week: Annotated[int, Field(ge=1,le=7)] | None = None
    duration_months: Annotated[int, Field(ge=1,le=24)] | None = None
    skills: Annotated[list[Annotated[str, Field(min_length=1,max_length=40)]], Field(max_length=20)] = []

    @field_validator('direction')
    @classmethod
    def trimmed_direction(cls,value): return value.strip()

    @field_validator('cities')
    @classmethod
    def valid_cities(cls,value):
        if any(not city.strip() for city in value): raise ValueError('city_required')
        return list(dict.fromkeys(city.strip() for city in value))

    @field_validator('skills')
    @classmethod
    def valid_skills(cls,value):
        if any(not x.strip() for x in value):raise ValueError('skill_required')
        return list(dict.fromkeys(x.strip() for x in value))


class GoalInput(Contract):
    intent: Intent = 'auto'
    request: Request = 'auto'
    # F07Z-166: this training turn explicitly excludes the selected position's bound resume.
    use_resume: bool = True
    continue_after_research: bool = False
    message: Annotated[str, Field(min_length=1, max_length=2000)]
    jd_text: Annotated[str, Field(max_length=2500)] = ''
    conditions: Conditions = Field(default_factory=Conditions)

    @field_validator('message')
    @classmethod
    def nonblank(cls, value):
        if not value.strip(): raise ValueError('message_required')
        return value


class GoalReply(Contract):
    intent: Intent | None = None
    request: Request | None = None
    use_resume: bool | None = None
    continue_after_research: bool = False
    message: Annotated[str, Field(min_length=1, max_length=2000)]
    expected_version: PositiveInt
    conditions: Conditions | None = None
    jd_text: Annotated[str, Field(max_length=2500)] | None = None

    @field_validator('message')
    @classmethod
    def nonblank(cls, value):
        return GoalInput.nonblank(value)


class SelectJob(Contract):
    continue_on_selection: bool = False
    expected_version: PositiveInt
    run_id: UuidString
    snapshot_id: UuidString
    acknowledge_unknown: bool = False


class ManualJobInput(Contract):
    """A job the user adds by hand, for companies the two built-in sources cannot reach.

    This is not a scrape and not a guess: every field here is what the user typed.
    The stored snapshot is marked `user-jd`, and the same city/recruitment checks
    run on it as on a fetched job, so a self-added posting cannot quietly bypass
    the goal's own conditions.
    """
    expected_version: PositiveInt
    company: Annotated[str, Field(min_length=1, max_length=80)]
    title: Annotated[str, Field(min_length=1, max_length=120)]
    cities: Annotated[list[Annotated[str, Field(min_length=1, max_length=30)]], Field(max_length=5)] = []
    recruitment_label: Annotated[str, Field(max_length=20)] = ''
    url: str = ''
    jd_text: Annotated[str, Field(min_length=1, max_length=20000)]

    @field_validator('company','title','jd_text')
    @classmethod
    def present(cls,value):
        if not value.strip(): raise ValueError('required')
        return value.strip()

    @field_validator('url')
    @classmethod
    def https_only(cls,value):
        value=value.strip()
        if not value: return ''
        if not value.startswith('https://'): raise ValueError('https_required')
        if len(value)>500: raise ValueError('too_long')
        return value


class ManualJobRevision(ManualJobInput):
    """Edit the JD of an existing self-added position; `expected_version` is the position version.

    The position id stays the same, so a resume binding survives a typo fix. A new immutable
    snapshot is appended and the position's current pointer moves to it.
    """
    pass


class BindingInput(Contract):
    """Pick the resume version a position uses; `expected_version` is the position version."""
    expected_version: PositiveInt
    draft_id: UuidString


class GroundedField(Contract):
    value: Annotated[str, Field(min_length=1, max_length=80)]
    message_id: UuidString
    quote: Short

    @field_validator('value','quote')
    @classmethod
    def nonblank(cls,value):
        if not value.strip(): raise ValueError('nonblank_evidence_required')
        return value


class ExtractedFields(Contract):
    direction: GroundedField | None = None
    cities: Annotated[list[GroundedField], Field(max_length=5)] = []
    recruitment: GroundedField | None = None


Sha256 = Annotated[str, Field(pattern=r'^[a-f0-9]{64}$')]

class ApplicationChoice(Contract):
    application_id: UuidString
    context_sha256: Sha256
    action_id: Sha256


class ApplicationPlan(ApplicationChoice):
    application_version: PositiveInt
    package_revision: PositiveInt
    package_sha256: Sha256
    kind: Annotated[str, Field(max_length=40)]
    reason: Annotated[str, Field(max_length=200)]
    evidence_refs: Annotated[list[str], Field(max_length=6)]
    execution: Literal['proposal_only'] = 'proposal_only'


class ApplicationSummary(Contract):
    id: UuidString
    version: PositiveInt
    company: str
    title: str
    status: str


class Decision(Contract):
    type: Literal['ask_user','prepare_job_research','prepare_training','prepare_materials','prepare_application_action','finish']
    text: Annotated[str, Field(min_length=1, max_length=400)]
    fields: ExtractedFields = Field(default_factory=ExtractedFields)
    application: ApplicationChoice | None = None
    application_plan: ApplicationPlan | None = None


class GoalMessage(Contract):
    id: UuidString
    text: str
    origin: Literal['user','tool_result'] = 'user'


class GoalContext(Contract):
    intent: Intent = 'auto'
    goal_id: UuidString
    user_id: UuidString
    version: PositiveInt
    messages: list[GoalMessage]
    conditions: Conditions
    jd_text: str
    previous_actions: list[Decision] = []
    research_feedback: list[dict] = []
    applications: Annotated[list[ApplicationSummary], Field(max_length=30)] = []
    workflow_feedback: dict = Field(default_factory=dict)
    completion_trigger: dict | None = None


def validate_decision(context: GoalContext, value) -> Decision:
    candidate = Decision.model_validate(value)
    allowed={'research':{'prepare_job_research','ask_user'},'materials':{'prepare_materials','ask_user'},
             'training':{'prepare_training','ask_user'},'review':{'prepare_application_action','ask_user'},'finish':{'finish'}}
    if context.intent!='auto' and candidate.type not in allowed[context.intent]:raise ValueError('explicit_intent_conflict')
    if candidate.type=='prepare_job_research' and context.completion_trigger and context.completion_trigger['kind']=='research' and context.completion_trigger['status'] in ('SUCCEEDED','PARTIAL'):
        raise ValueError('research_result_already_available')
    if candidate.type=='prepare_application_action':
        if not candidate.application or candidate.application.application_id not in {a.id for a in context.applications}:
            raise ValueError('application_not_in_scope')
        if candidate.fields!=ExtractedFields():raise ValueError('application_action_cannot_extract_profile')
    elif candidate.application is not None or candidate.application_plan is not None:
        raise ValueError('application_action_required')
    texts = {m.id:m.text for m in context.messages if m.origin=='user'}
    fields = [candidate.fields.direction,candidate.fields.recruitment,*candidate.fields.cities]
    for field in fields:
        if field and (field.message_id not in texts or field.quote not in texts[field.message_id]
                      or field.value.casefold() not in field.quote.casefold()):
            raise ValueError('ungrounded_extraction')
    if candidate.type in ('prepare_training','prepare_materials') and not (context.jd_text.strip() or context.conditions.direction or candidate.fields.direction):
        raise ValueError('training_target_required')
    if candidate.type == 'prepare_job_research' and not (context.conditions.direction or candidate.fields.direction):
        raise ValueError('research_direction_required')
    # Displayed material state is server-owned, just as application action text is.
    material=context.workflow_feedback.get('materials',{})
    if candidate.type=='prepare_materials' and 'source_count' in material:
        count=material['source_count'];confirmed=material.get('confirmed_fact_count',0)
        if count:
            candidate.text=(f'已上传{count}份材料，已有{confirmed}条用户确认事实（未经外部核验）。'
                            '已准备材料处理提议，请确认后执行；可直接生成简历优化初稿，无需逐条确认后再出稿。'
                            '若选择单独提取经历条目，新提议事实仍需另行确认，无需重复上传已有材料。')
        else:
            candidate.text='尚未上传材料。已准备材料处理提议，请先添加材料再确认执行；若只想按岗位训练，可以不上传简历。'
    return candidate
