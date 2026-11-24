"""Non-executable written questions use the original session lifecycle and SQL locks."""
import random
import re
from uuid import uuid4,uuid5,NAMESPACE_URL
from psycopg.types.json import Jsonb
from interview_agent.storage.auth import require_session
from interview_agent.storage.errors import ApiError
from interview_agent.career.repository import digest
from interview_agent.materials.repository import MaterialRepository
from interview_agent.question_banks.repository import BankRepository,summary
from .contracts import creation_body


async def create(repo,user,gid,key,value):
    async with repo.pool.connection() as conn,conn.transaction():
        await require_session(conn,user);goal=await repo._root(conn,gid,user.user_id)
        hashed=digest(creation_body(value))
        old=await (await conn.execute('SELECT id,request_hash FROM app.career_written_sessions WHERE goal_id=%s AND request_id=%s',(gid,key))).fetchone()
        if old:
            if old['request_hash']!=hashed:raise ApiError('idempotency_conflict',409)
            return str(old['id']),False
        if goal['version']!=value.expected_version or goal['status']=='CANCELLED':raise ApiError('version_conflict',409)
        existing=await (await conn.execute('SELECT * FROM app.career_written_sessions WHERE goal_id=%s ORDER BY id FOR UPDATE',(gid,))).fetchall()
        if len(existing)>=20:raise ApiError('written_session_limit',409)
        for s in existing:
            current=await repo._refresh(conn,goal,s)
            pending=await (await conn.execute("SELECT 1 FROM app.career_written_submissions WHERE session_id=%s AND status IN ('PENDING','RUNNING')",(s['id'],))).fetchone()
            if current['status']=='ACTIVE' or pending:raise ApiError('written_in_progress',409)
        resolver=BankRepository(repo.pool)
        banks={r.bank_id:await resolver._version(conn,user.user_id,r.bank_id,r.version,True) for r in sorted(value.question_banks,key=lambda r:r.bank_id)}
        questions=[];chosen=[]
        for ref in value.question_banks:
            bank=banks[ref.bank_id];items=[i for i in bank['document']['items'] if value.role in i['roles'] and i['kind']!='java_programming']
            if not items:raise ApiError('question_bank_role_not_supported',400)
            chosen.append(summary(bank))
        if value.item_ids:
            # F07Z-89: the user picked the questions; keep their order and refuse a list this
            # bank/role cannot satisfy instead of silently falling back to the first N.
            index={}
            for ref in value.question_banks:
                bank=banks[ref.bank_id]
                for item in bank['document']['items']:
                    if item['item_id'] in value.item_ids:
                        if item['item_id'] in index:
                            raise ApiError('written_item_ambiguous',400)
                        index[item['item_id']]=(bank,item)
            for item_id in value.item_ids:
                found=index.get(item_id)
                if found is None:raise ApiError('written_item_not_found',400)
                bank,item=found
                if value.role not in item['roles'] or item['kind']=='java_programming':
                    raise ApiError('written_item_role_not_supported',400)
                identity='q-'+uuid5(NAMESPACE_URL,bank['bank_id']+':'+str(bank['version'])+':'+item['item_id']).hex
                questions.append({'question_id':identity,'item':item,'bank_item':{**summary(bank),'item_id':item['item_id'],'item_sha256':digest(item)}})
            policy='explicit_items_in_user_order'
            available=len(questions);seed=None
        else:
            # F07Z-91: a filtered (and optionally shuffled) pool. Duplicate item ids across the
            # two named banks count once - the same question must not fill two slots.
            pool=[];seen=set()
            for ref in value.question_banks:
                bank=banks[ref.bank_id]
                for item in bank['document']['items']:
                    if value.role not in item['roles'] or item['kind']=='java_programming':continue
                    if value.item_kinds and item['kind'] not in value.item_kinds:continue
                    if item['item_id'] in seen:continue
                    seen.add(item['item_id']);pool.append((bank,item))
            if value.item_kinds and not pool:raise ApiError('question_bank_kind_not_supported',400)
            available=len(pool);seed=None
            if value.shuffle:
                # A random sample cannot be larger than its pool; the non-shuffled path keeps the
                # original "max_questions is a cap" semantics and simply takes what exists.
                if len(pool)<value.max_questions:raise ApiError('written_selection_too_few_items',400)
                seed=uuid4().hex
                picked=random.Random(seed).sample(pool,value.max_questions)
                policy='filtered_random_items_v1' if value.item_kinds else 'random_items_from_bank_v1'
            else:
                picked=pool[:value.max_questions]
                policy='filtered_first_items_v1' if value.item_kinds else 'first_role_matching_items_in_user_order'
            for bank,item in picked:
                identity='q-'+uuid5(NAMESPACE_URL,bank['bank_id']+':'+str(bank['version'])+':'+item['item_id']).hex
                questions.append({'question_id':identity,'item':item,'bank_item':{**summary(bank),'item_id':item['item_id'],'item_sha256':digest(item)}})
        payload={'kind':'question_set','question_set':questions[:value.max_questions],'question_banks':chosen,
                 'target':await MaterialRepository(repo.pool)._target(conn,goal),
                 'selection_basis':{'origin':'explicit_bank_versions','policy':policy,
                                    'available_questions':available,'selected_questions':len(questions),
                                    **({'item_kinds':value.item_kinds} if value.item_kinds else {}),
                                    **({'seed':seed} if seed else {})},
                 'policy':{'version':'written-question-set-v1','role':value.role,'mode':'independent','system_ai_help':False,
                           'requires_code_execution':False,'duration_seconds':value.duration_seconds,
                           'external_assistance_verified':False,'answer_key_verification':'declared_not_verified'}}
        from .context import enrich
        await enrich(conn,repo,gid,value,payload)
        row=await (await conn.execute('''INSERT INTO app.career_written_sessions(goal_id,goal_version,request_id,request_hash,input_payload,deadline_at)
            VALUES(%s,%s,%s,%s,%s,clock_timestamp()+(%s*interval '1 second')) RETURNING id''',
            (gid,goal['version'],key,hashed,Jsonb(payload),value.duration_seconds))).fetchone()
        return str(row['id']),True


async def answer(repo,user,gid,sid,key,value):
    async with repo.pool.connection() as conn,conn.transaction():
        await require_session(conn,user);goal,s=await repo._session(conn,gid,sid,user.user_id)
        if s['input_payload'].get('kind')!='question_set':raise ApiError('written_question_set_required',400)
        hashed=digest(value.model_dump(mode='json'))
        old=await (await conn.execute('SELECT id,request_hash FROM app.career_written_answers WHERE session_id=%s AND request_id=%s',(sid,key))).fetchone()
        if old:
            if old['request_hash']!=hashed:raise ApiError('idempotency_conflict',409)
            return str(old['id']),False
        if s['status']!='ACTIVE' or s['version']!=value.expected_version:raise ApiError('version_conflict',409)
        q=next((q for q in s['input_payload']['question_set'] if q['question_id']==value.question_id),None)
        if q is None:raise ApiError('written_question_not_in_scope',400)
        item=q['item']
        if item['kind'] in ('single_choice','multiple_choice'):
            allowed={o['option_id'] for o in item['options']}
            if value.text or not set(value.option_ids)<=allowed or (item['kind']=='single_choice' and len(value.option_ids)>1):
                raise ApiError('invalid_written_choice_answer',400)
        elif value.option_ids:raise ApiError('text_answer_required',400)
        count=await (await conn.execute('SELECT count(*) AS n FROM app.career_written_answers WHERE session_id=%s',(sid,))).fetchone()
        if count['n']>=100:raise ApiError('written_answer_limit',409)
        row=await (await conn.execute('''INSERT INTO app.career_written_answers(session_id,answer_seq,question_id,request_id,request_hash,answer)
            VALUES(%s,%s,%s,%s,%s,%s) RETURNING id''',(sid,s['version'],value.question_id,key,hashed,Jsonb({'option_ids':value.option_ids,'text':value.text})))).fetchone()
        await conn.execute('UPDATE app.career_written_sessions SET version=version+1,revision=revision+1 WHERE id=%s',(sid,))
        return str(row['id']),True


async def answer_history(conn,sid):
    return await (await conn.execute('SELECT id,answer_seq,question_id,answer FROM app.career_written_answers WHERE session_id=%s ORDER BY answer_seq',(sid,))).fetchall()


# F07Z-71: sentences that are notes about the product, not part of what a good answer contains.
_META_POINT_MARKERS=('不自动进行语义评分','不能直接外推','不代表能力认证','本题不')
_POINT_SPLIT=re.compile(r'[；;。\n]+')


def derive_reference_points(item):
    """Return the author's points for a subjective item.

    Explicit `reference_points` win; otherwise the free-text `reference` is split on the
    punctuation the built-in banks already use. This is a pure read-side function: nothing is
    written back into the bank document, so `content_sha256` and request hashes stay unchanged,
    and no model is involved, so the result is reproducible offline.
    """
    declared=[p.strip() for p in (item.get('reference_points') or []) if p and p.strip()]
    if declared:return declared[:6]
    text=(item.get('reference') or '').replace('\r\n','\n')
    points=[]
    for part in _POINT_SPLIT.split(text):
        part=part.strip()
        if not part or any(marker in part for marker in _META_POINT_MARKERS):continue
        if part not in points:points.append(part)
    return points[:6]


def grade(questions,history):
    latest={a['question_id']:a for a in history};items=[];keyed=matched=0
    for q in questions:
        item=q['item'];event=latest.get(q['question_id']);a=event['answer'] if event else {'option_ids':[],'text':''}
        present=bool(a['option_ids'] or a['text'].strip())
        keys=item.get('correct_option_ids',[])
        points=derive_reference_points(item)
        if item['kind'] in ('single_choice','multiple_choice'):
            if keys:keyed+=1
            status='unanswered' if not present else 'ungraded_missing_key' if not keys else 'matches_declared_key' if set(a['option_ids'])==set(keys) else 'does_not_match_declared_key'
            if status=='matches_declared_key':matched+=1
        else:status='unanswered' if not present else 'requires_human_review' if points else 'recorded_without_reference'
        items.append({'question_id':q['question_id'],'bank_item':q['bank_item'],'answer':a,'answer_event_id':str(event['id']) if event else None,
                      'status':status,'declared_correct_option_ids':keys,'reference':item['reference'],
                      # F07Z-71: the checklist a reviewer can tick by hand. Derived, never scored.
                      'reference_points':points})
    return {'items':items,'declared_key_question_count':keyed,'declared_key_match_count':matched,
            'answer_key_verification':'declared_not_verified','text_semantic_grading_performed':False,
            'ability_certified':False,'external_assistance_verified':False}


async def ensure_report(conn,s):
    if s['status']!='COMPLETED' or s['input_payload'].get('kind')!='question_set':return
    old=await (await conn.execute('SELECT 1 FROM app.career_written_reports WHERE session_id=%s',(s['id'],))).fetchone()
    if old:return
    report=grade(s['input_payload']['question_set'],await answer_history(conn,s['id']))
    await conn.execute('INSERT INTO app.career_written_reports(session_id,payload,content_sha256) VALUES(%s,%s,%s)',(s['id'],Jsonb(report),digest(report)))


async def view(conn,s):
    history=await answer_history(conn,s['id']);latest={a['question_id']:a for a in history}
    report=await (await conn.execute('SELECT payload,content_sha256 FROM app.career_written_reports WHERE session_id=%s',(s['id'],))).fetchone()
    questions=[]
    for q in s['input_payload']['question_set']:
        item=q['item'];event=latest.get(q['question_id'])
        questions.append({'question_id':q['question_id'],'kind':item['kind'],'title':item['title'],'question':item['question'],
                          'options':item.get('options',[]),'bank_item':q['bank_item'],'answer':event['answer'] if event else None})
    return {'id':str(s['id']),'goal_version':s['goal_version'],'version':s['version'],'revision':s['revision'],'status':s['status'],
            'deadline_at':s['deadline_at'],'stop_reason':s['stop_reason'],'kind':'question_set','policy':s['input_payload']['policy'],
            'target':s['input_payload']['target'],'application':s['input_payload'].get('application'),'retest_observation_id':(s['input_payload'].get('retest_source') or {}).get('observation_id'),'selection_basis':s['input_payload']['selection_basis'],
            'question_banks':s['input_payload']['question_banks'],'questions':questions,
            'answer_events':[{'id':str(a['id']),'answer_seq':a['answer_seq'],'question_id':a['question_id'],'answer':a['answer']} for a in history],
            'report':{**report['payload'],'content_sha256':report['content_sha256']} if report and s['status']=='COMPLETED' else None}
