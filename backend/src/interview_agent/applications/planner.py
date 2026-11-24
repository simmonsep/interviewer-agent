"""Deterministic preparation advice with references, no model or external side effects."""
def review(package,events,current,training=(),written=()):
    ref='package:'+str(current['package_revision']); warnings=[]; actions=[]
    def warn(code,text,refs=None): warnings.append({'code':code,'text':text,'evidence_refs':refs or [ref]})
    def action(kind,reason,refs=None): actions.append({'kind':kind,'reason':reason,'evidence_refs':refs or [ref]})
    progress=[e for e in events if e['kind']=='progress']
    availability=[e for e in events if e['kind']=='availability']
    refs=['event:'+e['id'] for e in progress[-1:]] or [ref]
    facts=[{'kind':'recorded_status','text':current['status'],'verification':'user_reported_not_external_verified','evidence_refs':refs}]
    changed=package['goal_version']!=current['goal_version'] or current['goal_cancelled']
    closed=bool(availability and availability[-1]['payload']['availability']=='closed')
    if changed: warn('target_changed','当前目标已改变；保留原申请档案。')
    if package['material_revision']!=current['material_revision']: warn('material_changed','当前材料版本与本申请包不同，历史包未被替换。')
    if closed: warn('job_closed_user_reported','用户报告岗位关闭，尚未外部核验。',['event:'+availability[-1]['id']])
    if current['status']=='DRAFT':
        if changed: action('review_target','先确认原岗位是否仍符合当前目标。')
        if closed: action('verify_availability','先核对岗位状态再决定是否提交。',['event:'+availability[-1]['id']])
        if package['material']['kind']=='none': action('prepare_material','尚未绑定实际申请材料；按岗位要求准备或明确无需材料。')
        if not package['entry_url']: action('confirm_entry','补充用户核对的官网申请入口。')
        if not changed and not closed:
            action('manual_submission','按岗位要求由用户在官网提交；系统不会代投或标为已提交。')
    elif current['status']=='SUBMITTED': action('wait_for_update','保留用户确认，未回复不等于拒绝。',refs)
    elif current['status']=='INTERVIEW': action('prepare_interview','按实际投递包与用户记录的轮次准备面试。',refs)
    elif current['status']=='OFFER': action('review_offer','核对用户提供的录用条件，缺失信息继续确认。',refs)
    elif current['status'] in ('REJECTED','WITHDRAWN'): action('reflect','对照材料与已知反馈复盘；当前记录不能证明拒绝原因。',refs)
    else: action('verify_progress','当前进度未知，等待用户补充证据。',refs)
    hypotheses=[]
    for o in training:
        refs=['session:'+o['session_id'],'observation:'+o['id'],'answer:'+o['answer_id'],'package:'+str(o['binding']['package_revision'])]
        p=o['payload']
        hypotheses.append({'label':p['label'],'summary':p['summary'],'topic_id':p['topic_id'],'assisted':p['assisted'],
                           **({'resume_source':p['evidence_source']['resume_source']} if p.get('evidence_source',{}).get('resume_source') else {}),
                           **({'summary_origin':p['summary_origin']} if 'summary_origin' in p else {}),
                           'origin':p['origin'],'verification':'training_observation_not_rejection_cause','binding':o['binding'],'evidence_refs':refs})
        if p['label'] in ('needs_probe','inconclusive'):
            action('retest_topic','训练观察待新情景核验，不能据此解释招聘结果。',refs)
    for o in written:
        refs=o['evidence_refs']+['package:'+str(o['application']['package_revision'])]
        if o['outcome'] in ('WRONG_ANSWER','COMPILE_ERROR','RUNTIME_ERROR','TIME_LIMIT','MEMORY_LIMIT','OUTPUT_LIMIT','does_not_match_declared_key'):
            action('retest_written','依据所记录历史包/轮次的笔试结果，准备新情景核验提议；不能据此解释招聘结果。',refs)
        elif o['outcome'] in ('requires_human_review','recorded_without_reference','ungraded_missing_key','unanswered'):
            action('review_written','本项未获正确性核验，请先复核原答卷，不推断能力薄弱。',refs)
    return {'origin':'rule_based_ja08','package_revision':current['package_revision'],
            'package_sha256':current['package_sha256'],'facts':facts,'warnings':warnings,'actions':actions,'training_hypotheses':hypotheses}
