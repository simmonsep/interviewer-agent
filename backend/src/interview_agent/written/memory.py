"""Explicit, source-derived written observations; original results stay immutable."""
from typing import Annotated
from pydantic import Field
from psycopg.types.json import Jsonb
from interview_agent.contracts.common import Contract
from interview_agent.storage.auth import require_session
from interview_agent.storage.errors import ApiError
from interview_agent.career.repository import digest
from interview_agent.interviews.repository import InterviewRepository


class Publish(Contract):
    expected_version: Annotated[int,Field(ge=1)]
    source_id: Annotated[str,Field(min_length=1,max_length=80)]


def effective(row):
    p=dict(row['payload'])
    if p.get('comparison'):p['comparison']={**p['comparison'],'source_active':row['comparison_source_active']}
    return p


async def evidence_at(conn,gid,aid=None):
    rows=await (await conn.execute('''SELECT o.id,o.payload,o.created_at,
        NOT EXISTS(SELECT 1 FROM app.career_written_retractions p WHERE p.observation_id::text=o.payload#>>'{comparison,observation_id}') AS comparison_source_active
        FROM app.career_written_observations o WHERE o.goal_id=%s
        AND (%s::text IS NULL OR o.payload#>>'{application,id}'=%s)
        AND NOT EXISTS(SELECT 1 FROM app.career_written_retractions r WHERE r.observation_id=o.id)
        ORDER BY o.created_at,o.id''',(gid,aid,aid))).fetchall()
    return [{'id':str(r['id']),**{k:v for k,v in effective(r).items() if k!='source'},
             'recorded_at':r['created_at'].isoformat(),
             'evidence_refs':['written-observation:'+str(r['id']),'written-session:'+r['payload']['session_id'],
                              'written-source:'+r['payload']['source_id']]} for r in rows]


async def active(conn,gid,role):
    rows=await (await conn.execute('''SELECT o.id,o.payload,o.created_at,
        NOT EXISTS(SELECT 1 FROM app.career_written_retractions p WHERE p.observation_id::text=o.payload#>>'{comparison,observation_id}') AS comparison_source_active
        FROM app.career_written_observations o
        WHERE o.goal_id=%s AND o.payload->>'role'=%s AND NOT EXISTS
        (SELECT 1 FROM app.career_written_retractions r WHERE r.observation_id=o.id)
        ORDER BY o.created_at DESC,o.id DESC LIMIT 6''',(gid,role))).fetchall()
    return [{'id':str(r['id']),**effective(r),'recorded_at':r['created_at'].isoformat()} for r in reversed(rows)]


async def publish(repo,user,gid,sid,key,value):
    async with repo.pool.connection() as conn,conn.transaction():
        await require_session(conn,user);goal,s=await repo._session(conn,gid,sid,user.user_id)
        hashed=digest([sid,value.model_dump()])
        old=await (await conn.execute('SELECT id,request_hash FROM app.career_written_observations WHERE goal_id=%s AND request_id=%s',(gid,key))).fetchone()
        if old:
            if old['request_hash']!=hashed:raise ApiError('idempotency_conflict',409)
            return str(old['id']),False
        if s['version']!=value.expected_version:raise ApiError('version_conflict',409)
        if s['status']!='COMPLETED' or s['input_payload'].get('kind')=='programming_validation':raise ApiError('completed_practice_required',409)
        if await (await conn.execute("SELECT 1 FROM app.career_written_submissions WHERE session_id=%s AND status IN ('PENDING','RUNNING')",(sid,))).fetchone():raise ApiError('written_execution_in_progress',409)
        if await (await conn.execute('SELECT 1 FROM app.career_written_observations WHERE session_id=%s AND source_id=%s',(sid,value.source_id))).fetchone():raise ApiError('observation_already_published',409)
        data=s['input_payload'];policy=data['policy'];kind=data.get('kind','builtin_programming')
        if data.get('retest_source'):
            old=await (await conn.execute('SELECT 1 FROM app.career_written_retractions WHERE observation_id=%s',(data['retest_source']['observation_id'],))).fetchone()
            if old:raise ApiError('retest_source_retracted',409)
        if kind=='question_set':
            report=await (await conn.execute('SELECT payload,content_sha256 FROM app.career_written_reports WHERE session_id=%s',(sid,))).fetchone()
            item=next((i for i in report['payload']['items'] if i['question_id']==value.source_id),None) if report else None
            if not item:raise ApiError('resource_unavailable',404)
            source={'report_sha256':report['content_sha256'],**item}
            role=policy['role'];outcome=item['status'];topic=item['bank_item']['item_id']
            qualification='declared_key_or_ungraded_text_not_ability'
        else:
            row=await (await conn.execute('SELECT id,status,result,source_sha256,execution_started FROM app.career_written_submissions WHERE session_id=%s AND id::text=%s',(sid,value.source_id))).fetchone()
            if not row:raise ApiError('resource_unavailable',404)
            if row['status']!='SUCCEEDED' or not row['execution_started'] or not row['result'] or row['result']['verdict'] in ('ENVIRONMENT_ERROR','CANCELLED'):raise ApiError('verified_execution_result_required',409)
            source={'submission_id':str(row['id']),'source_sha256':row['source_sha256'],'result_sha256':digest(row['result']),
                    'task_sha256':data['task'].get('sha256') or digest(data['task']),'task_version':data['task']['version'],'bank_item':data['task'].get('bank_item')}
            role=policy.get('role','java-backend');outcome=row['result']['verdict'];topic=data['task']['topic_id']
            qualification='passed_or_failed_declared_cases_not_ability'
        payload={'session_id':sid,'source_id':value.source_id,'goal_version':s['goal_version'],'kind':kind,'role':role,
                 'topic_id':topic,'outcome':outcome,'source':source,'policy':policy,
                 'qualification':qualification,'origin':'source_derived_written_result','ability_certified':False,
                 'source_session_created_at':s['created_at'].isoformat()}
        if data.get('application'):payload['application']=data['application']
        if data.get('retest_source'):
            from .context import comparison
            payload['comparison']=comparison(data,source,outcome)
        row=await (await conn.execute('''INSERT INTO app.career_written_observations(goal_id,session_id,source_id,request_id,request_hash,payload)
            VALUES(%s,%s,%s,%s,%s,%s) RETURNING id''',(gid,sid,value.source_id,key,hashed,Jsonb(payload)))).fetchone()
        await bump(conn,repo,gid)
        return str(row['id']),True


async def bump(conn,repo,gid):
    await InterviewRepository(repo.pool)._memory_state(conn,gid)
    await conn.execute('UPDATE app.career_interview_memory_state SET revision=revision+1 WHERE goal_id=%s',(gid,))


async def listing(repo,user,gid):
    async with repo.pool.connection() as conn,conn.transaction():
        await require_session(conn,user);await repo._root(conn,gid,user.user_id)
        rows=await (await conn.execute('''SELECT o.id,o.payload,o.created_at,r.reason AS retraction_reason,
            NOT EXISTS(SELECT 1 FROM app.career_written_retractions p WHERE p.observation_id::text=o.payload#>>'{comparison,observation_id}') AS comparison_source_active
            FROM app.career_written_observations o
            LEFT JOIN app.career_written_retractions r ON r.observation_id=o.id WHERE o.goal_id=%s ORDER BY o.created_at,o.id''',(gid,))).fetchall()
        return {'items':[{'id':str(r['id']),**effective(r),'recorded_at':r['created_at'].isoformat(),'retraction_reason':r['retraction_reason']} for r in rows]}


async def retract(repo,user,gid,oid,value):
    async with repo.pool.connection() as conn,conn.transaction():
        await require_session(conn,user);await repo._root(conn,gid,user.user_id)
        row=await (await conn.execute('SELECT id FROM app.career_written_observations WHERE id=%s AND goal_id=%s',(oid,gid))).fetchone()
        if not row:raise ApiError('resource_unavailable',404)
        old=await (await conn.execute('SELECT reason FROM app.career_written_retractions WHERE observation_id=%s',(oid,))).fetchone()
        if old:
            if old['reason']!=value.reason:raise ApiError('observation_already_retracted',409)
            return
        await conn.execute('INSERT INTO app.career_written_retractions(observation_id,reason) VALUES(%s,%s)',(oid,value.reason))
        await bump(conn,repo,gid)
