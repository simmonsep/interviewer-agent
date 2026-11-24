import pytest
import interview_agent.interviews.worker as worker_module
from interview_agent.interviews.agent import ScriptedInterviewer
from test_postgres import arun,pg_settings
from test_worker import worker_settings
from test_material_repository import material_context
from test_core_quality_repository import prepared
from test_interview_repository import create,view,answer,process
from test_topic_preparation import Plan

pytestmark=[pytest.mark.postgres,pytest.mark.redis]


@pytest.mark.parametrize('pattern',['next_only','current_only'])
def test_preparation_and_safe_missing_role_diagnostics_survive_worker_sql_api(material_context,worker_settings,monkeypatch,pattern):
    c,g=material_context;fid=prepared(c,g,worker_settings)
    s=create(c,g,fact_ids=[fid]).json();arun(process(worker_settings));s=view(c,g,s)
    assert answer(c,g,s,'我实现了事务请求记录，验证时点仍需说明。').status_code==202
    class Probe(Plan):
        def __init__(self,scope,admit):
            async def noop():pass
            d=ScriptedInterviewer(scope,noop).decision
            self.s=scope;self.d=d.model_copy(update={'topic_id':'http-idempotency','question_id':'http-idempotency-base'})
            self.pattern=pattern;self.n=0;self.feedback=[];self.admit=admit
        async def complete(self,messages,tools):
            await self.admit();return await super().complete(messages,tools)
    monkeypatch.setattr(worker_module,'ScriptedInterviewer',Probe)
    arun(process(worker_settings));s=view(c,g,s)
    if pattern=='next_only':assert s['status']=='WAITING_FOR_USER' and s['run']['model_requests']==3
    else:
        assert s['status']=='FAILED' and s['run']['model_requests']==4
        error=next(t for t in s['run']['diagnostic']['tool_log'] if t['status']=='error')
        assert error['missing_topic_reads']=={'current':False,'next':True}
        assert '我实现了事务请求记录' not in str(s['run']['diagnostic'])
