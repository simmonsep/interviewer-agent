from uuid import uuid4
import pytest
from test_postgres import arun,pg_settings,query,client_for,signup
from test_worker import worker_settings
from test_material_repository import material_context,source,view,post,process
from test_resume_rewrite_repository import begin

pytestmark=[pytest.mark.postgres,pytest.mark.redis]


def prepare(c,g,w):
    source(c,g);assert begin(c,g).status_code==202;arun(process(w))
    return view(c,g)['drafts'][0]


def revision(c,g,d,action='accept',version=1,key=None,**kw):
    return post(c,f"/api/career-goals/{g['id']}/materials/drafts/{d['id']}/revisions",
                {'expected_version':1,'expected_revision':1,'expected_draft_version':version,'action':action,**kw},key)


def test_edit_select_accept_restore_and_export_preserve_history(material_context,worker_settings):
    c,g=material_context;original=prepare(c,g,worker_settings)
    items=[{**original['payload']['rewrite_items'][0],'text':'实现SQL事务与Redis通知，补充异常处理测试。','change_note':'用户完善技术细节。'}]
    k=uuid4().hex;r=revision(c,g,original,'edit',key=k,items=items,selected_suggestion_indexes=[0]);assert r.status_code==201,r.text
    edited=r.json()['drafts'][0]
    assert edited['run_id'] is None and edited['payload']['content_origin']=='user_revision'
    assert edited['payload']['selected_suggestions']==original['payload']['suggestions']
    assert '补充异常处理测试' in edited['markdown'] and not edited['payload']['facts']
    assert revision(c,g,original,'edit',key=k,items=items,selected_suggestion_indexes=[0]).json()['draft_id']==edited['id']
    assert revision(c,g,original,'edit',key=k,items=items).json()['code']=='idempotency_conflict'
    a=revision(c,g,edited,version=2);assert a.status_code==201,a.text
    accepted=a.json()['drafts'][0];assert accepted['payload']['candidate_status']=='user_selected'
    restored=revision(c,g,original,'restore',version=3);assert restored.status_code==201,restored.text
    d=restored.json()['drafts'][0];assert d['version']==4 and d['markdown']==original['markdown']
    assert c.get(f"/api/career-goals/{g['id']}/materials/drafts/{d['id']}.md").text==original['markdown']
    assert len(view(c,g)['drafts'])==4 and not view(c,g)['facts']
    assert next(x for x in view(c,g)['drafts'] if x['id']==original['id'])==original


def test_suggestions_never_appear_in_the_rendered_body(material_context,worker_settings):
    """External review invariant (F07Z-17): suggestions are unconfirmed candidates and must
    never be rendered into the body. `contracts.render_draft` builds the markdown from
    rewrite_items only; this pins it so a future "merge for convenience" breaks a test."""
    c,g=material_context;d=prepare(c,g,worker_settings)
    suggestions=d['payload']['suggestions']
    assert suggestions,'fixture carries no suggestions; the test would prove nothing'
    for text in suggestions:
        assert text not in d['markdown']


def test_conflicts_invalid_suggestions_and_source_scope(material_context,worker_settings):
    c,g=material_context;d=prepare(c,g,worker_settings);item=d['payload']['rewrite_items'][0]
    assert revision(c,g,d,version=2).json()['code']=='draft_version_conflict'
    assert revision(c,g,d,'edit').json()['code']=='invalid_revision_items'
    assert revision(c,g,d,'edit',items=[item],selected_suggestion_indexes=[3]).json()['code']=='suggestion_not_available'
    assert revision(c,g,d,'edit',items=[{**item,'source_ids':[str(uuid4())]}]).json()['code']=='invalid_revision_items'
    assert revision(c,g,d).status_code==201
    assert revision(c,g,d,version=2).json()['code']=='draft_version_conflict'
    source(c,g,revision=1)
    r=post(c,f"/api/career-goals/{g['id']}/materials/drafts/{d['id']}/revisions",
           {'expected_version':1,'expected_revision':2,'expected_draft_version':2,'action':'restore'})
    assert r.json()['code']=='material_draft_stale'


def test_revision_is_owner_scoped_and_does_not_mutate_application_package(material_context,worker_settings,pg_settings):
    from test_application_repository import create
    c,g=material_context;d=prepare(c,g,worker_settings)
    app=create(c,g,expected_material_revision=1,draft_id=d['id'])
    assert revision(c,g,d).status_code==201
    reread=c.get(f"/api/career-goals/{g['id']}/applications/{app['id']}").json()
    assert reread['package']==app['package']
    with client_for(pg_settings[0]) as other:
        other.headers['Origin']=pg_settings[0].origin;uid=signup(other)['user']['id']
        assert revision(other,g,d).status_code==404
    query(pg_settings[1],'DELETE FROM auth.users WHERE id=%s RETURNING id',(uid,))


RESUME='''教育经历
2014.09-2019.07 本科 某大学 计算机科学与技术
工作经历
2018.10-2019.02 某公司 前端开发实习生
项目经历
即时聊天系统 2017.08 - 2017.08
技能
Node / Vue / MySQL'''


def test_owner_can_correct_a_structured_line_without_touching_the_narrative(material_context,worker_settings):
    """The panel shows the four code-owned sections; this pins the backend half: a
    correction lands as a new version, the narrative is untouched, and a blanket
    clear is still refused by the same guards the model has to satisfy.

    Since F07Z-34 the scripted worker fills the sections by source segment ID, so this
    also proves the structured text is copied from the source line, not retyped."""
    c,g=material_context;assert source(c,g,text=RESUME).status_code==201
    assert begin(c,g).status_code==202;arun(process(worker_settings))
    d=view(c,g)['drafts'][0];items=d['payload']['rewrite_items']
    assert d['payload']['education']==['2014.09-2019.07 本科 某大学 计算机科学与技术']
    assert d['payload']['experience']==['2018.10-2019.02 某公司 前端开发实习生']
    assert d['payload']['projects']==['即时聊天系统 2017.08 - 2017.08']
    assert d['payload']['coverage']['unaccounted']==0
    assert d['payload']['structured_segment_ids']==['S002','S004','S006','S008']
    r=revision(c,g,d,'edit',items=items,education=['2014.09-2019.07 本科 某大学 计算机科学与技术（GPA 3.8）'],
               experience=d['payload']['experience'],projects=d['payload']['projects'],skills=d['payload']['skills'])
    assert r.status_code==201,r.text
    edited=r.json()['drafts'][0]
    assert edited['payload']['education']==['2014.09-2019.07 本科 某大学 计算机科学与技术（GPA 3.8）']
    assert 'GPA 3' in edited['markdown'] and edited['payload']['rewrite_items']==items
    assert edited['payload']['content_origin']=='user_revision'
    # the coverage ledger survives user edits (F07Z-38)
    assert edited['payload']['coverage']['unaccounted']==0
    assert edited['payload']['unsupported_omissions']==[]
    assert all(entry['audited'] is False for entry in edited['payload']['omission_audit'])
    cleared=revision(c,g,edited,'edit',version=2,items=items,education=[],experience=[],
                     projects=[],skills=[])
    assert cleared.json()['code']=='invalid_revision_items'
    assert revision(c,g,edited,'accept',version=2,education=['2014.09-2019.07 本科 某大学']).json()['code']=='sections_require_edit'
    assert revision(c,g,edited,'restore',version=2,skills=['Node / Vue / MySQL']).json()['code']=='sections_require_edit'


def test_a_user_edit_keeps_its_wording_and_records_the_unsupported_claim(material_context,
                                                                       worker_settings):
    """S00Gc: the owner's words are never rewritten, but the evidence verdict is recorded."""
    c, g = material_context
    draft = prepare(c, g, worker_settings)
    item = draft['payload']['rewrite_items'][0]
    edited = item['text'].rstrip('。') + '，保障系统稳定运行。'
    response = revision(c, g, draft, 'edit',
                        items=[{**item, 'text': edited, 'change_note': '用户补充稳定性描述。'}])
    assert response.status_code == 201, response.text
    new = response.json()['drafts'][0]
    assert edited in new['markdown']  # the text is intact, not moved or rewritten
    findings = new['payload']['grounding_findings']
    assert [finding['code'] for finding in findings] == ['unsupported_outcome']
    assert findings[0]['origin'] == 'user_edit'
    assert findings[0]['evidence_segments'] == item['evidence_segments']

    # Accept/restore do not run the edit check: nothing was edited, so there is nothing to report.
    accepted = revision(c, g, new, version=2)
    assert accepted.status_code == 201, accepted.text
    assert 'grounding_findings' not in accepted.json()['drafts'][0]['payload']


def test_each_bullet_exposes_its_source_chain(material_context, worker_settings):
    """S00Gd: one line of the resume answers "which source, which segment, edited or not"."""
    c, g = material_context
    draft = prepare(c, g, worker_settings)
    rows = draft['provenance']
    assert len(rows) == len(draft['payload']['rewrite_items'])
    first = rows[0]
    assert first['transformation'] == 'model_rewrite'
    assert first['confirmation'] == 'model_draft_unconfirmed'
    assert first['sources'][0]['name'] == '项目.md'
    assert first['sources'][0]['content_sha256']
    assert first['segments'] and first['segments'][0]['text']
    assert first['finding'] is None

    item = draft['payload']['rewrite_items'][0]
    edited = item['text'].rstrip('。') + '，保障系统稳定运行。'
    response = revision(c, g, draft, 'edit',
                        items=[{**item, 'text': edited, 'change_note': '用户补充稳定性描述。'}])
    assert response.status_code == 201, response.text
    row = response.json()['drafts'][0]['provenance'][0]
    assert row['transformation'] == 'user_edit' and row['confirmation'] == 'user_edited'
    assert row['finding']['code'] == 'unsupported_outcome'

