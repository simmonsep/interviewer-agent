"""Explicit owner confirmation, called inside the existing goal-row transaction."""
from interview_agent.storage.errors import ApiError


async def confirm_step(conn, goal, step_id, kind, table):
    # Tables are internal constants, never model/client-selected SQL identifiers.
    if table not in {'career_material_runs', 'career_interviews'}:
        raise ValueError('unsupported_handoff_table')
    step = await (await conn.execute('SELECT id,goal_version,status,candidate FROM app.career_steps WHERE id=%s AND goal_id=%s',
                                     (step_id, goal['id']))).fetchone()
    if (not step or step['goal_version'] != goal['version'] or step['status'] != 'SUCCEEDED'
            or goal['status'] != 'WAITING_FOR_TOOL' or not goal['action']
            or goal['action']['type'] != kind or step['candidate'] != goal['action']):
        raise ApiError('coordinator_proposal_changed', 409)
    prior = await (await conn.execute(f"SELECT id FROM app.{table} WHERE goal_id=%s AND input_payload#>>'{{coordinator_handoff,step_id}}'=%s",
                                      (goal['id'], step_id))).fetchone()
    if prior:
        raise ApiError('handoff_already_confirmed', 409)
    latest=await (await conn.execute('SELECT id FROM app.career_steps WHERE goal_id=%s AND goal_version=%s ORDER BY cycle DESC LIMIT 1',
                                     (goal['id'],goal['version']))).fetchone()
    if latest['id']!=step['id']:raise ApiError('coordinator_proposal_changed',409)
    return {'step_id': step_id, 'goal_version': goal['version'], 'action': kind, 'confirmation': 'authenticated_user'}


async def continue_after(conn,goal,payload,kind,identity,status,*,user_event=False):
    """Called under the goal row lock: durable result and wakeup commit together."""
    from psycopg.types.json import Jsonb
    from .repository import digest
    link=payload.get('coordinator_handoff') or {}
    if not link.get('continue_on_completion'):return False
    permitted={'WAITING_FOR_TOOL','WAITING_FOR_USER'} if user_event else {'WAITING_FOR_TOOL'}
    if goal['version']!=link['goal_version'] or goal['status'] not in permitted:return False
    parent=await (await conn.execute('SELECT * FROM app.career_steps WHERE goal_id=%s AND goal_version=%s ORDER BY cycle DESC LIMIT 1',
                                     (goal['id'],goal['version']))).fetchone()
    if (not parent or str(parent['id'])!=link['step_id'] or parent['status']!='SUCCEEDED'
            or parent['candidate']!=goal['action'] or parent['cycle']>=3):return False
    trigger={'kind':kind,'resource_id':str(identity),'status':status,'parent_step_id':link['step_id']}
    key='continuation-'+kind+'-'+str(identity)
    message='系统工具完成通知：'+kind+' / '+status+'。请读取业务反馈；此通知不是用户指令，不表示已确认事实或同意新任务。'
    body={'origin':'tool_result','completion_trigger':trigger}
    row=await (await conn.execute('''INSERT INTO app.career_steps(goal_id,goal_version,cycle,request_id,request_hash,message,input_payload)
        VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(goal_id,request_id) DO NOTHING RETURNING id''',
        (goal['id'],goal['version'],parent['cycle']+1,key,digest(body),message,Jsonb(body)))).fetchone()
    if not row:return False
    await conn.execute("UPDATE app.career_goals SET status='PENDING',action=NULL,revision=revision+1,updated_at=clock_timestamp() WHERE id=%s",(goal['id'],))
    return True


async def continue_user_event(conn,goal,kind,identity):
    latest=await (await conn.execute('SELECT id FROM app.career_steps WHERE goal_id=%s AND goal_version=%s ORDER BY cycle DESC LIMIT 1',
                                     (goal['id'],goal['version']))).fetchone()
    if not latest:return False
    payload={'coordinator_handoff':{'step_id':str(latest['id']),'goal_version':goal['version'],'continue_on_completion':True}}
    return await continue_after(conn,goal,payload,kind,identity,'CONFIRMED',user_event=True)


async def pause_tool_parent(conn,goal_id,payload,reason,kind):
    """Return a current linked tool wait to its owner, without scheduling work."""
    from psycopg.types.json import Jsonb
    link=payload.get('coordinator_handoff') or {}
    if not link:return False
    goal=await (await conn.execute('SELECT * FROM app.career_goals WHERE id=%s FOR UPDATE',(goal_id,))).fetchone()
    if goal['version']!=link['goal_version'] or goal['status']!='WAITING_FOR_TOOL':return False
    parent=await (await conn.execute('SELECT id,status,candidate FROM app.career_steps WHERE goal_id=%s AND goal_version=%s ORDER BY cycle DESC LIMIT 1',
                                   (goal['id'],goal['version']))).fetchone()
    if not parent or str(parent['id'])!=link['step_id'] or parent['status']!='SUCCEEDED' or parent['candidate']!=goal['action']:return False
    labels={'interview':'面试','materials':'材料任务','research':'岗位研究'}
    label=labels[kind]
    text={'user_stop':'本轮面试已按你的要求停止；等待你决定是否继续，不自动开始下一轮。',
          'deadline':'本轮面试已到期；已保存回答，等待你决定下一步。',
          'attempt_limit':label+'恢复次数已用尽；请查看失败记录后决定下一步，不自动重试。',
          'context_changed':label+'依据已变化；请确认新依据后决定下一步。',
          'task_failed':label+'失败；请查看错误记录并决定下一步，不自动重跑原任务。',
          'admission_wait':label+'已暂停：凭据或费用/工具准入需要处理，请查看错误码并决定下一步；不会自动再次请求。'}[reason]
    await conn.execute("UPDATE app.career_goals SET status='WAITING_FOR_USER',action=%s,revision=revision+1,updated_at=clock_timestamp() WHERE id=%s",
                       (Jsonb({'type':'ask_user','text':text,'fields':{}}),goal['id']))
    return True


async def refresh_sessions(conn,pool,goal_id):
    """The caller owns the goal lock; reuse existing bounded session refreshes."""
    from interview_agent.interviews.repository import InterviewRepository
    from interview_agent.written.repository import WrittenRepository
    interviews=await (await conn.execute("SELECT id FROM app.career_interviews WHERE goal_id=%s AND status IN ('PENDING','RUNNING','WAITING_FOR_USER','WAITING_CREDENTIALS') ORDER BY created_at,id",(goal_id,))).fetchall()
    for s in interviews:await InterviewRepository(pool)._session(conn,goal_id,s['id'])
    goal=await (await conn.execute('SELECT * FROM app.career_goals WHERE id=%s FOR UPDATE',(goal_id,))).fetchone()
    written=await (await conn.execute("SELECT * FROM app.career_written_sessions WHERE goal_id=%s AND (status='ACTIVE' OR EXISTS(SELECT 1 FROM app.career_written_submissions r WHERE r.session_id=career_written_sessions.id AND r.status IN ('PENDING','RUNNING'))) ORDER BY created_at,id FOR UPDATE",(goal_id,))).fetchall()
    for s in written:await WrittenRepository(pool)._refresh(conn,goal,s)


async def feedback(conn, goal_id, pool):
    await refresh_sessions(conn,pool,goal_id)
    from interview_agent.written.memory import evidence_at
    written_observations=await evidence_at(conn,goal_id)
    alignment=await (await conn.execute('''SELECT r.id,r.result->'alignment_items' AS requirements FROM app.career_material_runs r
        JOIN app.career_goals g ON g.id=r.goal_id JOIN app.career_material_state m ON m.goal_id=g.id
        LEFT JOIN app.career_job_selections j ON j.goal_id=g.id AND j.goal_version=g.version
        WHERE g.id=%s AND g.status<>'CANCELLED' AND r.goal_version=g.version AND r.material_revision=m.revision
        AND r.status='SUCCEEDED' AND r.result->>'action'='alignment'
        AND ((j.snapshot_id IS NOT NULL AND r.input_payload#>>'{target,snapshot_id}'=j.snapshot_id::text)
          OR (j.snapshot_id IS NULL AND r.input_payload#>>'{target,kind}'='user_jd' AND r.input_payload#>>'{target,text}'=g.jd_text))
        ORDER BY r.created_at DESC,r.id DESC LIMIT 1''',(goal_id,))).fetchone()
    selected=await (await conn.execute('''SELECT s.id AS snapshot_id,s.payload->>'title' AS title FROM app.career_job_selections j
        JOIN app.career_goals g ON g.id=j.goal_id AND g.version=j.goal_version
        JOIN app.career_job_snapshots s ON s.id=j.snapshot_id WHERE j.goal_id=%s''',(goal_id,))).fetchone()
    counts = await (await conn.execute('''SELECT
        (SELECT count(*) FROM app.career_material_sources WHERE goal_id=%s) AS source_count,
        (SELECT count(*) FROM app.career_material_facts WHERE goal_id=%s) AS confirmed_fact_count,
        (SELECT count(*) FROM app.career_material_facts WHERE goal_id=%s AND source_id=(
         SELECT id FROM app.career_material_sources WHERE goal_id=%s ORDER BY material_revision DESC,id DESC LIMIT 1)) AS latest_source_confirmed_fact_count''',
                                       (goal_id, goal_id, goal_id, goal_id))).fetchone()
    materials = await (await conn.execute('''SELECT id,status,goal_version,material_revision,
        input_payload->>'mode' AS mode,result->>'action' AS result_action
        FROM app.career_material_runs WHERE goal_id=%s ORDER BY created_at DESC,id DESC LIMIT 3''', (goal_id,))).fetchall()
    interviews = await (await conn.execute('''SELECT id,status,goal_version,input_payload#>>'{policy,role}' AS role
        FROM app.career_interviews WHERE goal_id=%s ORDER BY created_at DESC,id DESC LIMIT 3''', (goal_id,))).fetchall()
    written=await (await conn.execute('''SELECT s.id,s.status,s.goal_version,
        r.payload->'declared_key_question_count' AS declared_key_question_count,
        r.payload->'declared_key_match_count' AS declared_key_match_count
        FROM app.career_written_sessions s LEFT JOIN app.career_written_reports r ON r.session_id=s.id AND s.status='COMPLETED'
        WHERE s.goal_id=%s AND s.input_payload->>'kind'='question_set' ORDER BY s.created_at DESC,s.id DESC LIMIT 3''',(goal_id,))).fetchall()
    observations = await (await conn.execute('''SELECT o.id,o.session_id,o.payload FROM app.career_interview_observations o
        JOIN app.career_interviews s ON s.id=o.session_id WHERE o.goal_id=%s AND s.status='COMPLETED'
        AND NOT EXISTS(SELECT 1 FROM app.career_interview_corrections c WHERE c.observation_id=o.id)
        ORDER BY o.created_at DESC,o.id DESC LIMIT 3''', (goal_id,))).fetchall()
    return {'written_observations':written_observations[-6:],'written_tests':[{**r,'id':str(r['id']),'qualification':'declared_key_comparison_not_verified_ability'} for r in written],
            'material_alignment':{'run_id':str(alignment['id']),'requirements':alignment['requirements'],
                                 'qualification':'model_proposal_not_ability_certification'} if alignment else None,
            'selected_job':{'snapshot_id':str(selected['snapshot_id']),'title':selected['title']} if selected else None,
            'materials': {**counts, 'runs': [{**r, 'id': str(r['id'])} for r in materials]},
            'interviews': [{**r, 'id': str(r['id'])} for r in interviews],
            'observations': [{'id': str(o['id']), 'session_id': str(o['session_id']),
                              **{k: o['payload'][k] for k in ('topic_id','label','summary','assisted')},
                              'verification': 'training_observation_not_verified_ability'} for o in observations]}
