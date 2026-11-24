"""Imported Java tasks reuse written execution; only reference runs qualify a version."""
import hashlib
from psycopg.types.json import Jsonb
from interview_agent.storage.auth import require_session
from interview_agent.storage.errors import ApiError
from interview_agent.career.repository import digest
from interview_agent.materials.repository import MaterialRepository
from interview_agent.question_banks.repository import BankRepository
from .contracts import creation_body


async def resolve(repo,conn,user,value):
    ref=value.programming_task
    bank=await BankRepository(repo.pool)._version(conn,user.user_id,ref.bank_id,ref.version,True)
    item=next((i for i in bank['document']['items'] if i['item_id']==ref.item_id),None)
    if not item or item['kind']!='java_programming':raise ApiError('java_programming_task_required',400)
    if value.role not in item['roles']:raise ApiError('question_bank_role_not_supported',400)
    program=item['programming']
    task={'id':'private-java-'+item['item_id'],'version':'bank-'+str(bank['version']),'language':'java21','entrypoint':'Main.java',
          'starter':program['starter'],'title':item['title'],'topic_id':'custom-java','statement':item['question'],
          'origin':'user_imported_declared_not_verified','analysis':item['reference'],'cases':program['cases'],
          'profile':program['profile'],'reference_sha256':hashlib.sha256(program['reference_solution'].encode()).hexdigest(),
          'bank_item':{**{k:bank[k] for k in ('bank_id','version','origin','content_sha256')},'name':bank['document']['name'],
                       'item_id':item['item_id'],'item_sha256':digest(item)}}
    task['sha256']=digest(task)
    return task,program['reference_solution']


async def qualified(conn,user_id,task,image):
    return await (await conn.execute('''SELECT r.id,s.id AS session_id FROM app.career_written_submissions r
        JOIN app.career_written_sessions s ON s.id=r.session_id JOIN app.career_goals g ON g.id=s.goal_id
        WHERE g.user_id=%s AND s.status='COMPLETED' AND s.input_payload->>'kind'='programming_validation'
        AND s.input_payload#>>'{task,sha256}'=%s AND s.input_payload#>>'{policy,image}'=%s
        AND s.input_payload#>>'{policy,execution_policy}'='ja06-docker-java-v1'
        AND r.source_sha256=%s AND r.execution_started=true AND r.status='SUCCEEDED' AND r.result->>'verdict'='ACCEPTED'
        ORDER BY r.created_at DESC LIMIT 1''',(user_id,task['sha256'],image,task['reference_sha256']))).fetchone()


async def create(repo,user,gid,key,value,*,validate=False):
    if repo.image is None:raise ApiError('written_executor_disabled',503)
    if value.programming_task is None:raise ApiError('java_programming_task_required',400)
    async with repo.pool.connection() as conn,conn.transaction():
        await require_session(conn,user);goal=await repo._root(conn,gid,user.user_id)
        body=creation_body(value)
        if validate:body['purpose']='programming_validation'
        hashed=digest(body)
        old=await (await conn.execute('SELECT id,request_hash FROM app.career_written_sessions WHERE goal_id=%s AND request_id=%s',(gid,key))).fetchone()
        if old:
            if old['request_hash']!=hashed:raise ApiError('idempotency_conflict',409)
            return str(old['id']),False
        if goal['version']!=value.expected_version or goal['status']=='CANCELLED':raise ApiError('version_conflict',409)
        sessions=await (await conn.execute('SELECT * FROM app.career_written_sessions WHERE goal_id=%s ORDER BY id FOR UPDATE',(gid,))).fetchall()
        if len(sessions)>=20:raise ApiError('written_session_limit',409)
        for session in sessions:
            current=await repo._refresh(conn,goal,session)
            pending=await (await conn.execute("SELECT 1 FROM app.career_written_submissions WHERE session_id=%s AND status IN ('PENDING','RUNNING')",(session['id'],))).fetchone()
            if current['status']=='ACTIVE' or pending:raise ApiError('written_in_progress',409)
        if goal['written_submissions']>=12:raise ApiError('written_execution_budget_exhausted',409)
        task,reference=await resolve(repo,conn,user,value)
        proof=None if validate else await qualified(conn,user.user_id,task,repo.image)
        if not validate and not proof:raise ApiError('programming_task_validation_required',409)
        payload={'kind':'programming_validation' if validate else 'imported_programming','task':task,
                 'target':await MaterialRepository(repo.pool)._target(conn,goal),
                 'selection_basis':{'origin':'explicit_programming_version','validation_submission_id':str(proof['id']) if proof else None},
                 'policy':{'version':'imported-java-v1','role':value.role,'mode':'reference_validation' if validate else 'independent',
                           'system_ai_help':False,'image':repo.image,'max_submissions':1 if validate else 3,'language':'java21',
                           'duration_seconds':value.duration_seconds,'execution_policy':'ja06-docker-java-v1','external_assistance_verified':False}}
        from .context import enrich
        await enrich(conn,repo,gid,value,payload)
        session=await (await conn.execute('''INSERT INTO app.career_written_sessions(goal_id,goal_version,request_id,request_hash,input_payload,deadline_at)
            VALUES(%s,%s,%s,%s,%s,clock_timestamp()+(%s*interval '1 second')) RETURNING *''',
            (gid,goal['version'],key,hashed,Jsonb(payload),value.duration_seconds))).fetchone()
        if validate:
            await conn.execute('''INSERT INTO app.career_written_submissions(session_id,goal_id,goal_version,seq,request_id,request_hash,source,source_sha256,input_payload)
                VALUES(%s,%s,%s,1,'reference-validation',%s,%s,%s,%s)''',
                (session['id'],gid,goal['version'],digest({'reference_sha256':task['reference_sha256']}),reference,task['reference_sha256'],Jsonb(payload)))
            await conn.execute('UPDATE app.career_written_sessions SET submissions=1 WHERE id=%s',(session['id'],))
            await conn.execute('UPDATE app.career_goals SET written_submissions=written_submissions+1,revision=revision+1 WHERE id=%s',(gid,))
        return str(session['id']),True
