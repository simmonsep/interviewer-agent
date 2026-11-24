"""Failure diagnostics through the original Worker/SQL/API; synthetic invalid submissions."""
import pytest
import interview_agent.interviews.worker as worker_module
from test_postgres import arun,pg_settings
from test_worker import worker_settings
from test_material_repository import material_context
from test_core_quality_repository import prepared
from test_interview_repository import create,view,process
from test_interview_diagnostics import InvalidSubmission
from test_core_quality import project

pytestmark=[pytest.mark.postgres,pytest.mark.redis]


@pytest.mark.parametrize('kind',['domain','schema'])
def test_rejected_error_details_survive_worker_sql_api_without_original_arguments(material_context,worker_settings,monkeypatch,kind):
    c,g=material_context;fid=prepared(c,g,worker_settings)
    s=create(c,g,fact_ids=[fid]).json()
    class Probe(InvalidSubmission):
        def __init__(self,scope,admit):
            payload=project(scope).model_dump()
            if kind=='domain':payload['question_text']='SYNTHETIC-PRIVATE-TEXT'
            else:
                payload['project_focus']='SYNTHETIC-SECRET-VALUE'
                payload['SYNTHETIC-SECRET-FIELD']='SYNTHETIC-SECRET-EXTRA'
            super().__init__(payload);self.admit=admit
        async def complete(self,messages,tools):
            await self.admit()
            return await super().complete(messages,tools)
    monkeypatch.setattr(worker_module,'ScriptedInterviewer',Probe)
    arun(process(worker_settings));v=view(c,g,s)
    assert v['status']=='FAILED' and v['events']==[]
    assert v['run']['error_code']=='interview_model_limit' and v['run']['model_requests']==4
    errors=[t for t in v['run']['diagnostic']['tool_log'] if t['status']=='error']
    expected='invalid_project_question' if kind=='domain' else 'invalid_interview_decision_schema'
    assert len(errors)==2 and all(t['error_code']==expected for t in errors)
    if kind=='schema':assert all(t['error_fields']==['project_focus','unknown'] for t in errors)
    assert 'SYNTHETIC-PRIVATE' not in str(v) and 'SYNTHETIC-SECRET' not in str(v)
