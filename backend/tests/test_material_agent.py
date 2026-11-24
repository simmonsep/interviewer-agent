import asyncio
from copy import deepcopy
from uuid import uuid4
import pytest
from interview_agent.materials.contracts import SourceInput,FactProposal,grounded,validate_result,MaterialResult,render_draft
from interview_agent.materials.agent import run_material_agent,ScriptedMaterials
from interview_agent.runtime.types import AssistantResponse,ToolCall


def scope():
    sid=str(uuid4());text='个人负责实现Redis通知与SQL事务提交。\n使用独立数据库验证取消后的旧结果无法提交。'
    return {'mode':'analyze','goal_version':1,'material_revision':1,'target':{'kind':'direction','direction':'Java后端'},
            'sources':[{'id':sid,'name':'项目.md','text':text,'content_sha256':'test'}],'facts':[]}


def run(value,provider,event=None):return asyncio.run(run_material_agent(value,provider,event or asyncio.Event()))


class Calls:
    def __init__(self,calls):self.calls=iter(calls);self.n=0;self.errors=[]
    async def complete(self,messages,tools):
        self.n+=1
        last=messages[-1]
        if last.role=='tool' and last.is_error:self.errors.append(last.text)
        elif last.role=='user' and 'invalid_material_tool' in last.text:self.errors.append(last.text)
        name,args=next(self.calls)
        return AssistantResponse(tool_calls=[ToolCall(id=str(self.n),name=name,arguments=args)])


@pytest.mark.parametrize('text',['','   ','abc\x00def','解析\ufffd失败'])
def test_reject_unreadable_material(text):
    with pytest.raises(ValueError):SourceInput(expected_version=1,expected_revision=0,name='简历.txt',format='text',text=text)


@pytest.mark.parametrize('name,format',[('resume.pdf','text'),('resume.docx','markdown'),('resume.txt','pdf')])
def test_explicit_format_boundary(name,format):
    with pytest.raises(ValueError):SourceInput(expected_version=1,expected_revision=0,name=name,format=format,text='abc')


def test_normalized_text_exact_offsets_and_no_fabricated_or_ambiguous_quote():
    s=scope();source=s['sources'][0];quote=source['text'].splitlines()[1]
    result=grounded(FactProposal(source_id=source['id'],kind='result',quote=quote),s['sources'])
    assert source['text'][result['start_char']:result['end_char']]==quote
    for text in ['把吞吐提升100倍','重复']:
        with pytest.raises(ValueError):grounded(FactProposal(source_id=source['id'],kind='result',quote=text),[{**source,'text':'重复，重复'}])
    with pytest.raises(ValueError):grounded(FactProposal(source_id=str(uuid4()),kind='skill',quote=quote),s['sources'])


def test_grounded_tolerates_retyped_whitespace_but_not_characters():
    text='官网项目：基于  Vue 全家桶 +FullPage+Element'
    identity=str(uuid4());sources=[{'id':identity,'text':text}]
    quote='基于 Vue 全家桶 + FullPage + Element'
    result=grounded(FactProposal(source_id=identity,kind='result',quote=quote),sources)
    assert text[result['start_char']:result['end_char']]=='基于  Vue 全家桶 +FullPage+Element'
    with pytest.raises(ValueError):
        grounded(FactProposal(source_id=identity,kind='result',quote='基于 Vue 全家桶 +FullPage+ElementX'),sources)


def test_analyze_prompt_cap_matches_the_contract():
    from interview_agent.materials.agent import SYSTEM_PROMPT
    assert '最多8条' in SYSTEM_PROMPT


def test_analysis_proposals_are_unconfirmed_and_exact():
    s=scope()
    async def admit():pass
    value=run(s,ScriptedMaterials(s,admit))
    assert value.action=='analysis' and len(value.proposals)==2 and not value.fact_ids
    assert [x['tool'] for x in value.tool_log]==['read_material_scope','read_source','submit_analysis']
    assert validate_result(s,value)==value
    bad=value.model_dump();bad['proposals'][0]['end_char']+=1
    with pytest.raises(ValueError):validate_result(s,bad)


def test_tool_order_citation_gate_and_permission_recovery():
    s=scope();sid=s['sources'][0]['id'];quote=s['sources'][0]['text'].splitlines()[0]
    provider=Calls([('shell',{'command':'ignored'}),('read_material_scope',{}),
                    ('submit_analysis',{'facts':[{'source_id':sid,'kind':'contribution','quote':quote}]}),
                    ('read_source',{'source_id':sid}),('submit_analysis',{'facts':[{'source_id':sid,'kind':'contribution','quote':quote}]})])
    value=run(s,provider)
    assert value.action=='analysis' and len(provider.errors)==2
    # the trace also carries a rejection `reason` since F07Z-21; assert what matters
    assert value.tool_log[0]['tool']=='unknown' and value.tool_log[0]['status']=='error'


def test_no_material_pauses_with_optional_training_path():
    s=scope();s['sources']=[]
    async def admit():pass
    value=run(s,ScriptedMaterials(s,admit))
    assert value.action=='ask_user' and '不提供简历' in value.question and not value.proposals


def test_draft_requires_confirmed_scoped_facts_and_escapes_active_markdown():
    s=scope();s['mode']='draft';fid=str(uuid4())
    s['facts']=[{'id':fid,'source_id':s['sources'][0]['id'],'kind':'contribution','quote':'个人负责<script>x</script>[链接](javascript:evil)','start_char':0,'end_char':1}]
    async def admit():pass
    result=run(s,ScriptedMaterials(s,admit));markdown,payload=render_draft(s,result)
    assert result.fact_ids==[fid] and '<script>' not in markdown and '\\[链接\\]' in markdown
    assert payload['facts'][0]['quote']==s['facts'][0]['quote'] and payload['target']==s['target']
    bad=result.model_dump();bad['fact_ids']=[str(uuid4())]
    with pytest.raises(ValueError):validate_result(s,bad)
    bad=result.model_dump();bad['fact_ids']=[fid,fid]
    with pytest.raises(ValueError):validate_result(s,bad)


def test_draft_cannot_read_jd_as_personal_fact_or_skip_target_inspection():
    s=scope();s['mode']='draft';fid=str(uuid4());s['facts']=[{'id':fid,'kind':'skill','quote':'Java'}]
    provider=Calls([('read_material_scope',{}),('create_draft',{'fact_ids':[fid]}),('read_confirmed_facts',{}),
                    ('create_draft',{'fact_ids':[fid]}),('read_target',{}),('create_draft',{'fact_ids':[fid]})])
    result=run(s,provider);assert result.action=='draft' and len(provider.errors)==2


def test_repeated_invalid_calls_are_bounded():
    s=scope();provider=Calls([('read_source',{'source_id':str(uuid4())})]*7)
    with pytest.raises(ValueError,match='material_model_limit'):run(s,provider)
    assert provider.n==6


def test_section_must_be_assigned_by_the_batch_that_closes_its_block():
    """F07Z-52: a missing section used to surface only in the final validation, after the
    whole request budget was spent (live run 3: ten requests, no skills, no draft)."""
    from interview_agent.materials.segments import scope_segments
    sid=str(uuid4())
    text='工作经历\n2019.03 某公司\n负责缓存改造\n技能\n- Python\n'
    s={'mode':'draft','draft_style':'rewrite','goal_version':1,'material_revision':1,
       'target':{'kind':'direction','direction':'Java 后端'},
       'sources':[{'id':sid,'name':'简历.md','text':text,'content_sha256':'test'}],'facts':[]}
    seg=[x for x in scope_segments(s) if not x['heading']]
    pick=lambda prefix:next(x['id'] for x in seg if x['text'].startswith(prefix))
    dated,work_line,skill=pick('2019.03'),pick('负责缓存改造'),pick('- Python')
    def submit(experience,items,skills=()):
        return {'items':[{'text':t,'source_ids':[sid],'change_note':'整理','evidence_segments':[s_]}
                         for t,s_ in items],'education':[],'experience':experience,'projects':[],
                'skills':list(skills),'suggestions':[]}

    class Late:
        def __init__(self):self.n=0;self.seen=[]
        async def complete(self,messages,specs):
            self.seen.append(messages[-1].text or '')
            self.n+=1
            if self.n==1:name,args='read_material_scope',{}
            elif self.n==2:name,args='read_target',{}
            elif self.n==3:name,args='read_source',{'source_id':sid}
            elif self.n==4:name,args='submit_rewrite',submit([dated],[('负责缓存改造',work_line)])
            else:name,args='submit_rewrite',submit([dated],[('负责缓存改造',work_line)],[skill])
            return AssistantResponse(tool_calls=[ToolCall(id=str(self.n),name=name,arguments=args)])

    provider=Late();result=run(s,provider)
    refusal=provider.seen[4]
    assert '"reason": "batch_incomplete"' in refusal and '"missing_sections": ["skills"]' in refusal,refusal
    assert provider.n==5 and result.skills==['- Python']


def test_unaccounted_entry_title_or_tech_stack_is_refused_before_it_hides_a_project():
    """F07Z-52: the live model left project two's title and tech stack with no destination,
    so its bullets rendered under project one. Being "not omitted" is not enough."""
    from interview_agent.materials.segments import scope_segments
    sid=str(uuid4())
    text=('项目经历\n1. A 平台｜2025.01-2025.03\n技术栈：FastAPI\n- 做了甲\n'
          '2. B 平台｜2024.01-2024.03\n技术栈：Redis\n- 做了乙\n技能\n- Python\n')
    s={'mode':'draft','draft_style':'rewrite','goal_version':1,'material_revision':1,
       'target':{'kind':'direction','direction':'Java 后端'},
       'sources':[{'id':sid,'name':'简历.md','text':text,'content_sha256':'test'}],'facts':[]}
    seg=[x for x in scope_segments(s) if not x['heading']]
    pick=lambda prefix:next(x['id'] for x in seg if x['text'].startswith(prefix))
    title1,tech1,bullet1=pick('1. A '),pick('技术栈：FastAPI'),pick('- 做了甲')
    title2,tech2,bullet2=pick('2. B '),pick('技术栈：Redis'),pick('- 做了乙')
    skill=pick('- Python')
    def submit(projects,items,skills=()):
        return {'items':[{'text':t,'source_ids':[sid],'change_note':'整理','evidence_segments':[e]}
                         for t,e in items],'education':[],'experience':[],'projects':projects,
                'skills':list(skills),'suggestions':[]}

    class Forgetting:
        def __init__(self):self.n=0;self.seen=[]
        async def complete(self,messages,specs):
            self.seen.append(messages[-1].text or '')
            self.n+=1
            if self.n==1:name,args='read_material_scope',{}
            elif self.n==2:name,args='read_target',{}
            elif self.n==3:name,args='read_source',{'source_id':sid}
            elif self.n==4:name,args='submit_rewrite',submit([title1,tech1],[('做了甲',bullet1)])
            else:name,args='submit_rewrite',submit([title1,tech1,title2,tech2],
                                                   [('做了甲',bullet1),('做了乙',bullet2)],[skill])
            return AssistantResponse(tool_calls=[ToolCall(id=str(self.n),name=name,arguments=args)])

    provider=Forgetting();result=run(s,provider)
    refusal=provider.seen[4]
    # One envelope lists everything the batch still owes (F07Z-52 route B):
    # the dated title, its tech stack and the still-empty skills section.
    assert '"reason": "batch_incomplete"' in refusal,refusal
    assert title2 in refusal and tech2 in refusal and '"missing_sections": ["skills"]' in refusal,refusal
    assert '"next_segments"' in refusal
    assert provider.n==5
    assert len(result.structured_sections['projects'])==4
    assert {e for item in result.rewrite_items for e in item.evidence_segments}=={bullet1,bullet2}


def test_omitting_a_dated_or_tech_line_lands_in_the_same_envelope():
    """F07Z-52/route B, second surface: after B the live model answered the envelope by
    omitting the line and met `dated_segment_omitted` / `tech_stack_segment_omitted`, i.e. a
    different rule. Omission of a mandatory line is now dropped and reported as "no destination"."""
    from interview_agent.materials.segments import scope_segments
    sid=str(uuid4())
    text='工作经历\n2019.03 某公司\n技术栈：Java、Redis\n负责缓存改造\n技能\n- Python\n'
    s={'mode':'draft','draft_style':'rewrite','goal_version':1,'material_revision':1,
       'target':{'kind':'direction','direction':'Java 后端'},
       'sources':[{'id':sid,'name':'简历.md','text':text,'content_sha256':'test'}],'facts':[]}
    seg=[x for x in scope_segments(s) if not x['heading']]
    pick=lambda prefix:next(x['id'] for x in seg if x['text'].startswith(prefix))
    dated,tech,work,skill=pick('2019.03'),pick('技术栈：Java'),pick('负责缓存改造'),pick('- Python')
    def submit(omit_extra=(),place_tech=False):
        omitted=[{'segment_id':x,'reason':'structural_noise'} for x in omit_extra]
        return {'items':[{'text':'负责缓存改造','source_ids':[sid],'change_note':'整理',
                          'evidence_segments':[work]}],
                'education':[],'experience':[dated]+([tech] if place_tech else []),
                'projects':[],'skills':[skill],'suggestions':[],'omitted_segments':omitted}

    class Omitting:
        def __init__(self):self.n=0;self.seen=[]
        async def complete(self,messages,specs):
            self.seen.append(messages[-1].text or '')
            self.n+=1
            if self.n==1:name,args='read_material_scope',{}
            elif self.n==2:name,args='read_target',{}
            elif self.n==3:name,args='read_source',{'source_id':sid}
            elif self.n==4:name,args='submit_rewrite',submit([tech])
            else:name,args='submit_rewrite',submit(place_tech=True)
            return AssistantResponse(tool_calls=[ToolCall(id=str(self.n),name=name,arguments=args)])

    provider=Omitting();result=run(s,provider)
    refusal=provider.seen[4]
    assert '"reason": "batch_incomplete"' in refusal,refusal
    assert tech in refusal and '技术栈行必须有去处' in refusal,refusal
    assert 'tech_stack_segment_omitted' not in refusal
    assert provider.n==5 and result.rewrite_items


def test_citing_a_structured_line_folds_into_the_same_envelope():
    """F07Z-52/ENG-28: the live model cited education lines as evidence three times and spent
    its budget on a bare `evidence_segment_is_structured`; the refusal must say which line and
    why, in the same envelope as the other gaps."""
    from interview_agent.materials.segments import scope_segments
    sid=str(uuid4())
    text='教育经历\n2022.09-2026.06 本科 某大学\n技能\n- Python\n- 熟悉 LangGraph\n'
    s={'mode':'draft','draft_style':'rewrite','goal_version':1,'material_revision':1,
       'target':{'kind':'direction','direction':'Java 后端'},
       'sources':[{'id':sid,'name':'简历.md','text':text,'content_sha256':'test'}],'facts':[]}
    seg=[x for x in scope_segments(s) if not x['heading']]
    pick=lambda prefix:next(x['id'] for x in seg if x['text'].startswith(prefix))
    degree,skill_py,skill_lg=pick('2022.09'),pick('- Python'),pick('- 熟悉 LangGraph')
    def submit(cite_degree,skills=(),items=()):
        evidence=[degree] if cite_degree else [x for x in items]
        return {'items':[{'text':'本科毕业于某大学','source_ids':[sid],'change_note':'整理',
                          'evidence_segments':evidence}] if evidence else [],
                'education':[degree],'experience':[],'projects':[],'skills':list(skills),'suggestions':[]}

    class Confused:
        def __init__(self):self.n=0;self.seen=[]
        async def complete(self,messages,specs):
            self.seen.append(messages[-1].text or '')
            self.n+=1
            if self.n==1:name,args='read_material_scope',{}
            elif self.n==2:name,args='read_target',{}
            elif self.n==3:name,args='read_source',{'source_id':sid}
            elif self.n==4:name,args='submit_rewrite',submit(True,[skill_py])
            else:name,args='submit_rewrite',submit(False,[skill_py],[skill_lg])
            return AssistantResponse(tool_calls=[ToolCall(id=str(self.n),name=name,arguments=args)])

    provider=Confused();result=run(s,provider)
    refusal=provider.seen[4]
    assert '"reason": "batch_incomplete"' in refusal and degree in refusal,refusal
    assert '不要再用作证据' in refusal,refusal
    assert provider.n==5 and result.rewrite_items


def test_missing_read_target_names_the_next_call_instead_of_generic_refusal():
    """F07Z-52: the live model resent a rejected submit eight times because the reason said
    only `material_tool_not_allowed`; a prerequisite miss must name the missing call."""
    from interview_agent.materials.segments import scope_segments
    sid=str(uuid4())
    text='工作经历\n2019.03 某公司\n负责缓存改造\n'
    s={'mode':'draft','draft_style':'rewrite','goal_version':1,'material_revision':1,
       'target':{'kind':'direction','direction':'Java 后端'},
       'sources':[{'id':sid,'name':'简历.md','text':text,'content_sha256':'test'}],'facts':[]}
    ids=[x['id'] for x in scope_segments(s) if not x['heading']]
    submit={'items':[{'text':'负责缓存改造','source_ids':[sid],'change_note':'整理',
                      'evidence_segments':[ids[1]]}],
            'education':[],'experience':[ids[0]],'projects':[],'skills':[],'suggestions':[]}

    class Skipping:
        def __init__(self):self.n=0;self.seen=[]
        async def complete(self,messages,specs):
            self.seen.append(messages[-1].text or '')
            self.n+=1
            if self.n==1:name,args='read_material_scope',{}
            elif self.n==2:name,args='read_source',{'source_id':sid}
            elif self.n==3:name,args='submit_rewrite',submit
            elif self.n==4:name,args='read_target',{}
            else:name,args='submit_rewrite',submit
            return AssistantResponse(tool_calls=[ToolCall(id=str(self.n),name=name,arguments=args)])

    provider=Skipping();result=run(s,provider)
    refusal=provider.seen[3]
    assert '"reason": "read_target_required"' in refusal and '"next_call": "read_target"' in refusal,refusal
    assert provider.n==5 and result.rewrite_items


def test_request_ceiling_is_derived_once_and_reported_to_the_model():
    """F07Z-51/ENG-24: the loop, the reported limit and the SQL guard must not drift apart."""
    from interview_agent.materials.segments import (MAX_REQUESTS, narrative_batches,
                                                    pre_heading_ids, request_ceiling,
                                                    scope_segments)
    assert (request_ceiling(0), request_ceiling(4), request_ceiling(50)) == (6, 10, MAX_REQUESTS)
    sid=str(uuid4())
    text='求职意向：AI Agent 研发\n\n技能\n\n'+'\n'.join(f'- 负责模块{i:02d}的接口设计与联调' for i in range(1,33))
    s={'mode':'draft','draft_style':'rewrite','goal_version':1,'material_revision':1,
       'target':{'kind':'direction','direction':'AI Agent'},
       'sources':[{'id':sid,'name':'简历.md','text':text,'content_sha256':'test'}],'facts':[]}
    segments=scope_segments(s);header=set(pre_heading_ids(segments))
    batches=narrative_batches([x for x in segments if x['id'] not in header])
    limit=request_ceiling(len(batches))

    class Recording:
        def __init__(self):
            async def admit():pass
            self.inner=ScriptedMaterials(s,admit);self.n=0;self.seen=[]
        async def complete(self,messages,specs):
            self.seen.append(messages[-1].text or '')
            response=await self.inner.complete(messages,specs);self.n+=1;return response

    provider=Recording();result=run(s,provider)
    assert result.rewrite_items and result.structured_sections['skills']
    assert provider.n==3+len(batches)<=limit, (provider.n, len(batches), limit)
    assert f'"model_requests": {limit}' in ' '.join(provider.seen)


def test_cancellation_prevents_any_model_call():
    event=asyncio.Event();event.set();provider=Calls([])
    with pytest.raises(ValueError,match='material_cancelled'):run(scope(),provider,event)
    assert provider.n==0


def test_real_provider_independent_read_batch_is_accepted_without_batching_writes():
    s=scope();s['mode']='draft';fid=str(uuid4());s['facts']=[{'id':fid,'source_id':s['sources'][0]['id'],'kind':'skill','quote':'SQL','start_char':0,'end_char':3}]
    class Batched:
        n=0
        async def complete(self,messages,tools):
            self.n+=1
            if self.n==1:calls=[ToolCall(id='scope',name='read_material_scope',arguments={})]
            elif self.n==2:calls=[ToolCall(id='target',name='read_target',arguments={}),ToolCall(id='facts',name='read_confirmed_facts',arguments={})]
            else:
                assert [m.tool_name for m in messages[-2:]]==['read_target','read_confirmed_facts']
                calls=[ToolCall(id='draft',name='create_draft',arguments={'fact_ids':[fid]})]
            return AssistantResponse(tool_calls=calls)
    provider=Batched();result=run(s,provider)
    assert result.action=='draft' and provider.n==3


def test_batch_cannot_combine_read_with_write_and_failure_has_safe_diagnostics():
    from interview_agent.materials.agent import MaterialAgentFailure
    s=scope()
    class Unsafe:
        n=0
        async def complete(self,messages,tools):
            self.n+=1
            if self.n==1:return AssistantResponse(tool_calls=[ToolCall(id='first',name='read_material_scope',arguments={})])
            return AssistantResponse(tool_calls=[ToolCall(id='read',name='read_source',arguments={'source_id':s['sources'][0]['id']}),ToolCall(id='write',name='submit_analysis',arguments={'private':'not-a-real-key'})])
    with pytest.raises(MaterialAgentFailure,match='material_response_batch_rejected') as error:run(s,Unsafe())
    assert error.value.diagnostic['response_tools'][-1]==['read_source','submit_analysis']
    assert 'not-a-real-key' not in str(error.value.diagnostic) and '负责实现' not in str(error.value.diagnostic)

def test_unaccounted_segment_gets_one_targeted_repair_round():
    """F07Z-45: a line the model left with no destination is asked for exactly once."""
    source_id = str(uuid4())
    scope_rewrite = {'mode': 'draft', 'draft_style': 'rewrite', 'goal_version': 1, 'material_revision': 1,
                     'target': {'kind': 'direction', 'direction': '前端'},
                     'sources': [{'id': source_id, 'name': '简历.md', 'content_sha256': 'x',
                                  'text': '工作经历\n2018.10 某公司\n负责缓存改造\n设计了查询接口'}],
                     'facts': []}
    item = lambda text, evidence: {'text': text, 'source_ids': [source_id], 'change_note': '整理。',
                                   'evidence_segments': evidence}

    class Repairing:
        def __init__(self):
            self.n = 0
            self.repair_note = None

        async def complete(self, messages, tools):
            self.n += 1
            last = messages[-1].text if messages else ''
            if self.n == 1:
                name, args = 'read_material_scope', {}
            elif self.n == 2:
                name, args = 'read_target', {}
            elif self.n == 3:
                name, args = 'read_source', {'source_id': source_id}
            elif self.n == 4:
                # covers S003 only; S004 is left with no destination
                name, args = 'submit_rewrite', {'items': [item('负责缓存改造。', ['S003'])],
                                                'education': [], 'experience': ['S002'],
                                                'projects': [], 'skills': [], 'suggestions': []}
            else:
                assert 'repair' in last, last
                self.repair_note = last
                name, args = 'submit_rewrite', {'items': [item('设计了查询接口。', ['S004'])],
                                                'education': [], 'experience': [],
                                                'projects': [], 'skills': [], 'suggestions': []}
            return AssistantResponse(tool_calls=[ToolCall(id=str(self.n), name=name, arguments=args)])

    provider = Repairing()
    result = run(scope_rewrite, provider)
    assert [i.text for i in result.rewrite_items] == ['负责缓存改造。', '设计了查询接口。']
    assert provider.repair_note and 'S004' in provider.repair_note
    assert provider.n == 5                    # one extra request for the repair, no loop


def test_a_final_validation_error_ends_the_run_with_its_own_code():
    """F07Z-126: after the last batch nothing can be corrected - the next submit would be
    refused with `all_batches_done`. Handing the error back only spends the remaining requests;
    resume 4 did exactly that and died as `material_model_limit` with no draft."""
    from interview_agent.materials.agent import MaterialAgentFailure
    from interview_agent.materials.segments import scope_segments
    sid = str(uuid4())
    s = {'mode': 'draft', 'draft_style': 'rewrite', 'goal_version': 1, 'material_revision': 1,
         'target': {'kind': 'direction', 'direction': 'Java 后端'},
         'sources': [{'id': sid, 'name': '简历.md', 'text': '负责接口改造。\n', 'content_sha256': 'test'}],
         'facts': []}
    only = next(x['id'] for x in scope_segments(s) if not x['heading'])

    class Flagged:
        def __init__(self):
            self.n = 0

        async def complete(self, messages, specs):
            self.n += 1
            if self.n == 1:
                name, args = 'read_material_scope', {}
            elif self.n == 2:
                name, args = 'read_target', {}
            elif self.n == 3:
                name, args = 'read_source', {'source_id': sid}
            else:
                name = 'submit_rewrite'
                args = {'items': [{'text': '负责接口改造，性能提升 300%。', 'source_ids': [sid],
                                   'change_note': '整理', 'evidence_segments': [only]}],
                        'education': [], 'experience': [], 'projects': [], 'skills': [], 'suggestions': []}
            return AssistantResponse(tool_calls=[ToolCall(id=str(self.n), name=name, arguments=args)])

    provider = Flagged()
    with pytest.raises(MaterialAgentFailure) as failure:
        run(s, provider)
    assert failure.value.code == 'claims_not_grounded'
    assert provider.n == 4, f'the terminal failure must not ask the model again: {provider.n}'
