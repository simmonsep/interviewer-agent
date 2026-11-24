"""Freeze optional historical application and explicit retest source at creation."""
from interview_agent.storage.errors import ApiError


async def enrich(conn,repo,gid,value,payload):
    if payload.get('kind')=='programming_validation' and (value.application_id or value.retest_observation_id):
        raise ApiError('reference_validation_cannot_bind_practice_context',400)
    if value.application_id:
        from interview_agent.applications.repository import ApplicationRepository
        c=await ApplicationRepository(repo.pool).training_context(conn,gid,value.application_id,value.application_package_revision)
        payload['application']={k:c[k] for k in ('id','package_revision','package_sha256')}
        payload['application'].update(interview_round=value.interview_round,company=c['package']['company'],title=c['package']['title'])
        payload['target']=c['package']['target']
    if value.retest_observation_id:
        o=await (await conn.execute('''SELECT o.id,o.payload FROM app.career_written_observations o WHERE o.id=%s AND o.goal_id=%s
            AND NOT EXISTS(SELECT 1 FROM app.career_written_retractions r WHERE r.observation_id=o.id)''',
            (value.retest_observation_id,gid))).fetchone()
        if not o:raise ApiError('retest_source_unavailable',409)
        p=o['payload']
        if p['role']!=payload['policy'].get('role','java-backend'):raise ApiError('retest_role_mismatch',400)
        # A cross-application attempt cannot inherit another application's performance.
        before=p.get('application');after=payload.get('application')
        if bool(before)!=bool(after) or (before and any(before[k]!=after[k] for k in ('id','package_revision','package_sha256'))):
            raise ApiError('retest_application_mismatch',400)
        payload['retest_source']={'observation_id':str(o['id']),'session_id':p['session_id'],'source_id':p['source_id'],
                                 'outcome':p['outcome'],'topic_id':p['topic_id'],'source':p['source'],'policy':p['policy']}


def comparison(data,source,outcome):
    old=data.get('retest_source')
    if not old:return None
    prior=old['source']
    def identity(item):return tuple(item.get(k) for k in ('bank_id','version','item_id','item_sha256'))
    same=(prior.get('task_sha256')==source.get('task_sha256')) if 'task_sha256' in source and 'task_sha256' in prior else (
        identity(prior['bank_item'])==identity(source['bank_item']) if source.get('bank_item') and prior.get('bank_item') else False)
    return {'observation_id':old['observation_id'],'prior_session_id':old['session_id'],'prior_outcome':old['outcome'],
            'current_outcome':outcome,'relation':'same_task_repetition' if same else 'different_task_not_calibrated',
            'prior_policy':old['policy'],'current_policy':data['policy'],'source_active':True,
            'performance_improvement':None,'qualification':'linked_attempts_not_learning_gain'}
