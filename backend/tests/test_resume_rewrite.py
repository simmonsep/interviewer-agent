from uuid import uuid4
import pytest
from interview_agent.materials.contracts import MaterialResult,render_draft,validate_result
from test_material_agent import scope,Calls,run

POLISHED='基于Spring Boot构建订单服务，结合Redis处理重复请求，围绕订单请求链路组织模块职责。'
SUGGESTION='可进一步设计幂等键的有效期和冲突处理，选择符合项目的方案后补充实现细节。'


def context():
    s=scope();s.update(mode='draft',draft_style='rewrite');s['sources'][0]['text']='我做了订单服务，用Spring Boot和Redis，处理重复请求。'
    return s


def candidate(s):return MaterialResult(action='draft',rewrite_items=[{'text':POLISHED,'source_ids':[s['sources'][0]['id']],'change_note':'将口语描述组织为技术与行动要点。'}],suggestions=[SUGGESTION])


def test_rewrite_accepts_paraphrasing_from_unconfirmed_source_and_exports_clean_copy():
    s=context();r=candidate(s);md,p=render_draft(s,r)
    assert POLISHED in md and '未核验' not in md and '事实 ' not in md and SUGGESTION not in md
    assert p['sources'][0]['text']==s['sources'][0]['text'] and p['rewrite_items'][0]['change_note']
    assert p['suggestions']==[SUGGESTION] and p['facts']==[]


def test_graph_requires_target_and_read_source_but_not_confirmed_facts():
    s=context();args={'items':[i.model_dump() for i in candidate(s).rewrite_items],'education':[],'experience':[],'projects':[],'skills':[],'suggestions':[SUGGESTION]}
    p=Calls([('read_material_scope',{}),('read_target',{}),('submit_rewrite',args),('read_source',{'source_id':s['sources'][0]['id']}),('submit_rewrite',args)])
    r=run(s,p);assert r.action=='draft' and r.rewrite_items[0].text==POLISHED and len(p.errors)==1


def test_rewrite_can_batch_target_and_source_reads():
    import asyncio
    from interview_agent.materials.agent import run_material_agent
    from interview_agent.runtime.types import AssistantResponse,ToolCall
    s=context();args={'items':[i.model_dump() for i in candidate(s).rewrite_items],'education':[],'experience':[],'projects':[],'skills':[]}
    class Provider:
        n=0
        async def complete(self,messages,specs):
            self.n+=1
            calls=([('read_material_scope',{})] if self.n==1 else
                   [('read_target',{}),('read_source',{'source_id':s['sources'][0]['id']})] if self.n==2 else
                   [('submit_rewrite',args)])
            return AssistantResponse(tool_calls=[ToolCall(id=f'{self.n}-{i}',name=n,arguments=a) for i,(n,a) in enumerate(calls)])
    r=asyncio.run(run_material_agent(s,Provider(),asyncio.Event()))
    assert r.action=='draft' and len(r.tool_log)==4


def test_foreign_source_and_wrong_mode_cannot_be_written():
    s=context();r=candidate(s);r.rewrite_items[0].source_ids=[str(uuid4())]
    with pytest.raises(ValueError):validate_result(s,r)
    s.pop('draft_style')
    with pytest.raises(ValueError):validate_result(s,candidate(s))


def test_formal_export_escapes_active_markup_and_keeps_notes_outside_body():
    s=context();r=candidate(s);r.rewrite_items[0].text='<script>evil</script>[x](javascript:evil)'
    md,p=render_draft(s,r);assert '<script>' not in md and '\\[x\\]' in md
