"""No provider calls: authorization guards and fixed diagnostic scope."""
import argparse
from pathlib import Path
import runpy
import pytest


def entry():return runpy.run_path(str(Path(__file__).resolve().parents[2]/'scripts/check-live-interview.py'))


@pytest.mark.parametrize('patch,code',[
    ({'mode':'live'},'live_requires_explicit_stage_approval_flags'),
    ({'mode':'live','execute_live':True,'max_cny':'2','credentials':Path('unused')},'live_requires_explicit_stage_approval_flags'),
    ({'credentials':Path('must-not-read')},'mock_must_not_read_real_credentials'),
    ({'mode':'live','mock_fault':'reject-once'},'live_must_not_use_mock_faults'),
])
def test_guards_precede_configuration_and_key_reads(patch,code):
    args=argparse.Namespace(mode='sdk-mock',execute_live=False,max_cny=None,credentials=None,mock_fault='none',
        api_config=Path('must-not-read'),worker_config=Path('must-not-read'),owner_config=Path('must-not-read'))
    for k,v in patch.items():setattr(args,k,v)
    with pytest.raises(ValueError,match=code):entry()['preflight'](args)


def test_new_single_role_policy_cannot_borrow_previous_ids_or_enlarge_attempt_cap():
    m=entry();p=m['InterviewPolicy'].load(m['ROOT']/'configs/core0606-interviews.json')
    assert m['PHASE']=='core-06-06-20261101-cny05'
    assert m['LIMIT']==500_000_000 and p.max_http_attempts==p.max_operation_attempts==4
    assert p.model=='deepseek-flash' and p.thinking=='disabled'



def test_bank_followup_has_independent_budget_and_rejects_old_approval():
    m=entry()
    policy=m['InterviewPolicy'].load(m['ROOT']/'configs/core0623-interviews.json')
    assert m['STAGES']['core-06-23'][0]=='core-06-23-20261109-cny06'
    assert policy.max_http_attempts==policy.max_operation_attempts==4
    assert 4*policy.reservation_nano==589_824_000 < m['BANK_LIMIT']==600_000_000
    args=argparse.Namespace(stage='core-06-23',mode='live',execute_live=True,
        max_cny='0.5',credentials=Path('must-not-read'),mock_fault='none')
    with pytest.raises(ValueError,match='live_requires_explicit_stage_approval_flags'):
        m['preflight'](args)


@pytest.mark.parametrize('fault,requests',[('none',3),('reject-once',4),('unknown',1)])
def test_bank_followup_sdk_consumes_blueprint_and_repair(fault,requests):
    import asyncio
    import httpx
    from interview_agent.interviews.agent import SYSTEM_PROMPT, interview
    from interview_agent.interviews.contracts import question_payload
    from interview_agent.billing.ledger import BillingUnavailable
    from interview_agent.runtime.deepseek import BudgetedTransport, DeepSeekProvider
    from test_bank_decision_repair import bank_scope

    m=entry()
    scope,next_topic=bank_scope()
    scope['answer']['text']=m['BANK_ANSWER']

    class Ledger:
        policy=m['InterviewPolicy'].load(m['ROOT']/'configs/core0623-interviews.json')

        async def reserve(self, claim, context, payload_hash, payload_bytes):
            return 'synthetic-reservation'

        async def settle(self, call_id, **result):
            return result.get('usage') is not None

    async def scenario():
        async def noop():
            pass
        scripted=m['ScriptedInterviewer'](scope,noop)
        async def handler(request):
            return await m['mock_completion'](request,scripted,fault,bank_followup=True)
        transport=BudgetedTransport(httpx.MockTransport(handler),Ledger(),None,scope)
        provider=DeepSeekProvider('offline-not-real',transport,system_prompt=SYSTEM_PROMPT)
        try:
            if fault=='unknown':
                with pytest.raises(BillingUnavailable,match='model_usage_unknown'):
                    await interview(scope,provider,asyncio.Event())
                return None,scripted.n
            return await interview(scope,provider,asyncio.Event()),scripted.n
        finally:
            await provider.aclose()

    decision,count=asyncio.run(scenario())
    assert count==requests
    if fault=='unknown':
        assert decision is None
        return
    assert decision.topic_id==next_topic
    assert question_payload(scope,decision)['text']==scope['pack']['topics'][next_topic]['base']
    assert decision.observation.answer_quote in scope['answer']['text']
    codes=[item['error_code'] for item in decision.tool_log if 'error_code' in item]
    assert codes==(['bank_question_text_is_frozen'] if fault=='reject-once' else [])
