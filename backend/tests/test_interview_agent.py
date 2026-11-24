import asyncio
from copy import deepcopy
from uuid import uuid4
import pytest
from interview_agent.interviews.catalog import pack,topic
from interview_agent.interviews.contracts import Decision,validate_decision,question_payload,CreateInterview
from interview_agent.interviews.agent import interview,ScriptedInterviewer,InterviewFailure
from interview_agent.runtime.types import AssistantResponse,ToolCall


def scope(mode='interview',answer=None):
    s={'policy':{'role':'java-backend','mode':mode,'version':'ja05-policy-v1','max_questions':3},'pack':pack('java-backend'),
       'target':{'kind':'direction','direction':'Java'},'facts':[],'prior_observations':[],'question_history':[],'current_question':None,'answer':None,'assisted_topics':[]}
    if answer is not None:
        q={'id':str(uuid4()),'topic_id':'java-concurrency','question_id':'java-concurrency-base','kind':'base','text':s['pack']['topics']['java-concurrency']['base'],'assisted':False,'fact_id':None}
        s.update(current_question=q,question_history=[q],answer={'id':str(uuid4()),'text':answer,'assisted':False})
    return s


def scripted(s):
    async def admit():pass
    return asyncio.run(interview(s,ScriptedInterviewer(s,admit),asyncio.Event()))


def observation(s):
    ref=topic(s,s['current_question']['topic_id'])['references'][0]
    return {'answer_id':s['answer']['id'],'label':'needs_probe','summary':'待通过情景追问核验。','answer_quote':s['answer']['text'],'evidence_id':ref['id'],'evidence_quote':ref['text'][:100]}


@pytest.mark.parametrize('role',['java-backend','ai-application'])
def test_role_packs_are_distinct_versioned_authored_and_not_scored(role):
    p=pack(role);assert len(p['topics'])==4 and len(p['sha256'])==64 and p['review']=='source_checked_draft' and p['semantic_scoring_eligible'] is False
    assert all(t['url'].startswith('https://') and all(t[k] for k in ('base','probe','retest','reference')) for t in p['topics'].values())
    assert ('java-concurrency' in p['topics'])==(role=='java-backend')


def test_no_material_first_question_and_actual_graph_sequence():
    s=scope();d=scripted(s);assert d.action=='ask_question' and d.question_id=='java-concurrency-base'
    assert [x['tool'] for x in d.tool_log]==['read_interview_scope','read_topics','submit_interview_action']
    assert question_payload(s,d)['assisted'] is False


def test_different_answer_evidence_selects_followup_or_next_topic():
    weak=scope(answer='只加volatile，我不清楚复合操作。');strong=scope(answer='count++是读改写，volatile只处理可见性，整体需要同步或原子类。')
    a=scripted(weak);b=scripted(strong)
    assert a.question_id=='java-concurrency-probe' and b.topic_id=='sql-isolation'
    assert a.observation.answer_quote==weak['answer']['text'] and b.observation.answer_quote==strong['answer']['text']


def test_coaching_records_explanation_and_new_retest_with_assistance():
    s=scope('coaching','不知道，只有volatile。');d=scripted(s);q=question_payload(s,d)
    assert d.action=='teach_and_retest' and q['kind']=='retest' and q['assisted'] and q['parent_question_id']==s['current_question']['id']
    assert q['text']!=s['current_question']['text'] and d.explanation
    s['policy']['mode']='interview'
    with pytest.raises(ValueError,match='coaching_policy_violation'):validate_decision(s,d)


@pytest.mark.parametrize('field,value',[('answer_quote','不存在的回答'),('answer_id',str(uuid4())),('evidence_id','foreign'),('evidence_quote','凭空补造的标准答案')])
def test_observations_require_current_answer_and_registered_reference(field,value):
    s=scope(answer='我不清楚。');o=observation(s);o[field]=value
    d={'action':'ask_question','topic_id':'java-concurrency','question_id':'java-concurrency-probe','observation':o}
    with pytest.raises(ValueError):validate_decision(s,d)


def test_question_cap_requires_finish_and_finish_retains_evidence():
    s=scope(answer='需要原子更新。');s['policy']['max_questions']=1;d=scripted(s)
    assert d.action=='finish' and d.observation.answer_id==s['answer']['id']
    with pytest.raises(ValueError,match='question_limit_finish_required'):validate_decision(s,{'action':'ask_question','topic_id':'sql-isolation','question_id':'sql-isolation-base','observation':observation(s)})


def test_repeat_question_and_cross_topic_probe_are_rejected():
    s=scope(answer='未确定。')
    for qid,tid in [('java-concurrency-base','java-concurrency'),('sql-isolation-probe','sql-isolation')]:
        with pytest.raises(ValueError):validate_decision(s,{'action':'ask_question','topic_id':tid,'question_id':qid,'observation':observation(s)})


def test_project_question_uses_only_confirmed_fact_and_separates_team_contribution():
    s=scope();fid=str(uuid4());s['facts']=[{'id':fid,'quote':'我参与了团队的Redis通知模块。','verification':'user_confirmed_not_external_verified'}]
    d=scripted(s);q=question_payload(s,d)
    assert d.topic_id=='project' and d.fact_id==fid and s['facts'][0]['quote'] in q['text'] and '区分' in q['text']
    invalid=d.model_dump();invalid['fact_id']=str(uuid4())
    with pytest.raises(ValueError):validate_decision(s,invalid)


def test_assisted_topic_stays_marked_after_explanation():
    s=scope(answer='这是一次回答。');s['assisted_topics']=['java-concurrency']
    d=Decision(action='ask_question',topic_id='java-concurrency',question_id='java-concurrency-probe',observation=observation(s))
    assert question_payload(s,validate_decision(s,d))['assisted']


def test_model_must_read_actual_topic_before_submission_and_can_repair():
    s=scope();d=Decision(action='ask_question',topic_id='java-concurrency',question_id='java-concurrency-base')
    class Repair:
        n=0
        async def complete(self,messages,tools):
            self.n+=1
            name,args=[('read_interview_scope',{}),('submit_interview_action',d.model_dump()),('read_topics',{'topic_ids':['java-concurrency']}),('submit_interview_action',d.model_dump())][self.n-1]
            return AssistantResponse(tool_calls=[ToolCall(id=str(self.n),name=name,arguments=args)])
    value=asyncio.run(interview(s,Repair(),asyncio.Event()));assert value.tool_log[1]['status']=='error' and value.action=='ask_question'


def test_invalid_tools_are_bounded_and_diagnostics_do_not_store_arguments():
    class Invalid:
        n=0
        async def complete(self,messages,tools):
            self.n+=1;return AssistantResponse(tool_calls=[ToolCall(id=str(self.n),name='shell',arguments={'secret':'never-store-this'})])
    p=Invalid()
    with pytest.raises(InterviewFailure,match='interview_model_limit') as caught:asyncio.run(interview(scope(),p,asyncio.Event()))
    assert p.n==5 and 'never-store-this' not in str(caught.value.diagnostic)


def test_cancel_before_start_sends_no_model_request():
    class Never:
        async def complete(self,*args):pytest.fail('cancelled before HTTP')
    event=asyncio.Event();event.set()
    with pytest.raises(InterviewFailure,match='interview_cancelled'):asyncio.run(interview(scope(),Never(),event))


@pytest.mark.parametrize('body',[{'mode':'written'},{'max_questions':13},{'duration_seconds':1},{'role':'random'}])
def test_policy_boundaries(body):
    with pytest.raises(ValueError):CreateInterview.model_validate({'expected_version':1,'role':'java-backend',**body})


def test_supported_large_context_fits_actual_sdk_request_without_truncating_answer(project_root):
    import httpx,json
    from interview_agent.interviews.billing import InterviewPolicy
    from interview_agent.runtime.deepseek import BudgetedTransport,DeepSeekProvider
    from interview_agent.interviews.agent import system_prompt_for
    from test_billing import reply
    s=scope(answer='原回答'+('保留原始语句。'*180)[:1490]);s['policy']['max_questions']=6
    from interview_agent.skills.registry import as_dict,select
    s['skill']=as_dict(select('interview','interview'))
    s['question_history']=[{**s['current_question'],'id':str(uuid4()),'text':'历史题干。'*80,'question_id':f'question-{i}'} for i in range(6)]
    s['facts']=[{'id':str(uuid4()),'quote':'用户声明。'*60,'verification':'user_confirmed_not_external_verified'} for _ in range(6)]
    s['policy']['feedback_summary']={'version':'project-extractive-v1'}
    s['target']={'kind':'user_jd','text':'岗位说明。'*500}
    s['prior_observations']=[{'id':str(uuid4()),'summary':'历史观察。'*40,'answer_quote':'旧回答。'*75,'pack_version':'old-v1','origin':'prior_observation_to_retest'} for _ in range(12)]
    sizes=[];views=[]
    class Ledger:
        policy=InterviewPolicy.load(project_root/'configs/ja05-deepseek-flash.json')
        async def reserve(self,claim,context,payload_hash,payload_bytes):sizes.append(payload_bytes);return str(uuid4())
        async def settle(self,*args,**kwargs):return True
    async def scenario():
        async def noop():pass
        scripted_provider=ScriptedInterviewer(s,noop)
        async def handler(request):
            payload=json.loads(request.content)
            if len(sizes)==2:views.append(json.loads(payload['messages'][-1]['content']))
            value=await scripted_provider.complete([],[])
            return httpx.Response(200,json=reply(value))
        provider=DeepSeekProvider('offline-not-real',BudgetedTransport(httpx.MockTransport(handler),Ledger(),None,s),system_prompt=system_prompt_for(s))
        try:return await interview(s,provider,asyncio.Event())
        finally:await provider.aclose()
    result=asyncio.run(scenario())
    assert result.action=='finish' and len(sizes)==3 and max(sizes)<=32768
    assert views[0]['answer']['text']==s['answer']['text'] and views[0]['facts']==s['facts']
    assert views[0]['model_view_truncation'] and len(views[0]['prior_observations'])<=6
