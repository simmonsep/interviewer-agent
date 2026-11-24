import asyncio
import json
from uuid import uuid4
import httpx
import pytest
from pydantic import ValidationError
from interview_agent.runtime.scripted import ScriptedProvider
from interview_agent.runtime.types import AssistantResponse,ToolCall
from interview_agent.career.research_agent import research
from interview_agent.career.research_repository import ResearchRepository
from interview_agent.career.contracts import Conditions
from interview_agent.career.matching import checks
from test_career_research_tools import handler


def scope():return ResearchRepository.scope({'conditions':{'direction':'Java','skills':['private-skill'],'auto_research':True},'jd_text':'private JD'},1)
def call(name,args=None):return AssistantResponse(tool_calls=[ToolCall(id=uuid4().hex,name=name,arguments=args or {})])
def execute(steps,respond=handler):
    provider=ScriptedProvider(steps)
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            return await research(scope(),provider,client,asyncio.Event(),origin='offline_fixture')
    return asyncio.run(run()),provider


def test_research_has_distinct_minimum_context_and_real_graph_tool_flow():
    s=scope();assert 'skills' not in s['conditions'] and 'jd_text' not in s and 'messages' not in s and 'user_id' not in s
    bundle,p=execute([call('read_research_scope'),call('search_jobs',{'keyword':'后台'}),call('read_job',{'job_id':'81001'}),call('finish_research')])
    assert bundle.agent.read_job_ids==['81001'] and len(p.requests)==4
    assert bundle.agent.query_observations[0]['status']=='ok' and bundle.agent.query_observations[0]['job_ids']==['81001','81002']
    assert [x['tool'] for x in bundle.agent.tool_log]==['read_research_scope','search_jobs','read_job','finish_research']


def test_role_violation_and_fabricated_job_are_tool_errors_not_external_reads():
    requests=[]
    def respond(r):requests.append(str(r.url));return handler(r)
    result,_=execute([call('send_resume',{'url':'https://evil.invalid'}),call('read_research_scope'),
                     call('search_jobs',{'keyword':'后台'}),call('read_job',{'job_id':'invented'}),
                     call('read_job',{'job_id':'81001'}),call('finish_research')],respond)
    assert [x['status'] for x in result.agent.tool_log].count('error')==2
    assert len(requests)==3 and all('evil' not in x for x in requests)


def test_source_failure_can_choose_question_and_no_hidden_retry():
    sent=[]
    def respond(r):sent.append(r);return httpx.Response(429)
    result,_=execute([call('read_research_scope'),call('search_jobs',{'keyword':'后台'}),call('ask_user',{'question':'是否提供JD？'})],respond)
    assert len(sent)==1 and result.agent.action=='ask_user' and result.search.status=='rate_limited'
    assert result.agent.query_observations==[{**result.agent.query_observations[0],'status':'rate_limited','keyword':'后台'}]


def test_two_query_limit_and_model_budget_are_independent():
    sent=[]
    def empty(r):sent.append(r);return httpx.Response(200,json={'code':'200','data':{'items':[]}})
    result,_=execute([call('read_research_scope'),call('search_jobs',{'keyword':'A'}),call('search_jobs',{'keyword':'B'}),call('search_jobs',{'keyword':'C'}),call('finish_research')],empty)
    assert len(sent)==2 and len(result.agent.queries)==2 and result.agent.tool_log[-2]['status']=='error'
    with pytest.raises(ValueError,match='model_limit'):execute([call('unknown') for _ in range(6)],empty)
    assert len(sent)==2


def test_expert_can_pause_without_query_and_cancellation_ends_graph():
    result,_=execute([call('read_research_scope'),call('ask_user',{'question':'请先确认方向。'})])
    assert result.agent.action=='ask_user' and result.executed_request is None
    async def cancelled():
        stop=asyncio.Event();stop.set()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(ValueError,match='cancel'):await research(scope(),ScriptedProvider([]),client,stop)
    asyncio.run(cancelled())


@pytest.mark.parametrize('requirements,user,field,expected',[
    ('2027届毕业生，每周至少4天，至少实习3个月。',{'graduation_year':2028},'graduation_year','conflict'),
    ('2026-2028届毕业生。',{'graduation_year':2027},'graduation_year','pass'),
    ('2026届、2028届毕业生。',{'graduation_year':2027},'graduation_year','conflict'),
    ('非 2027届毕业生。',{'graduation_year':2027},'graduation_year','unknown'),
    ('2027届及以前。',{'graduation_year':2026},'graduation_year','unknown'),
    ('2027届或有同等经验。',{'graduation_year':2028},'graduation_year','unknown'),
    ('每周至少4天。',{'days_per_week':3},'days_per_week','conflict'),
    ('每周3天以下。',{'days_per_week':4},'days_per_week','unknown'),
    ('每周四天优先。',{'days_per_week':3},'days_per_week','unknown'),
    ('至少实习3个月。',{'duration_months':4},'duration_months','pass'),
    ('实习至少6个月。',{},'duration_months','unknown'),
])
def test_limited_requirement_grammar_keeps_evidence_and_ambiguity(requirements,user,field,expected):
    result=checks({'requirements':requirements},user,3)
    row=next(x for x in result['checks'] if x['field']==field)
    assert row['status']==expected and row['source_quote'] in requirements and row['user_ref']['goal_version']==3


def test_missing_skills_is_unknown_not_conflict_and_optional_city_does_not_veto():
    job={'requirements':'熟悉Java、SQL、Redis。','cities':['北京市'],'recruitment_label':'实习生'}
    result=checks(job,{'cities':['上海'],'city_policy':'preferred','skills':['JavaScript'],'recruitment':'intern'},1)
    assert result['outcome']!='excluded'
    assert all(x['status']=='unknown' for x in result['checks'] if x['field']=='skills')
    assert result['score'] is None
    assert checks(job,{'recruitment':'campus'},1)['outcome']=='excluded'


def test_mixed_recruitment_and_imported_jd_evidence_are_not_misclassified():
    mixed=checks({'recruitment_label':'实习/应届生','requirements':'Java'},{'recruitment':'campus'},1)
    assert next(x for x in mixed['checks'] if x['field']=='recruitment')['status']=='pass'
    imported={'visible_text':'2027届毕业生，掌握Java。'}
    result=checks(imported,{'graduation_year':2027},2)
    for row in result['checks']:
        if row['source_quote']:assert row['source_quote'] in imported[row['source_path']]


@pytest.mark.parametrize('values',[{'auto_research':1},{'graduation_year':True},{'days_per_week':8},{'skills':['  ']}])
def test_confirmed_fact_contract_is_strict(values):
    with pytest.raises(ValidationError):Conditions.model_validate(values)
