"""Small authored role packs, not company questions or a validated scoring system."""

VERSION='ja05-role-pack-v1'
TOPICS={
 'redis-lock':{'title':'Redis锁的归属与过期','url':'https://redis.io/docs/latest/develop/clients/patterns/distributed-locks/',
  'reference':'锁值应标识持有者；释放时原子比较标识再删除，避免旧持有者删除他人的新锁。过期时间限定锁的有效期，业务执行超过租期需要另外处理。',
  'base':'用Redis保护一段业务，只使用SET NX拿锁还需要考虑什么？',
  'probe':'A的锁到期后B拿到锁，A随后释放锁。怎样避免A删掉B的锁？',
  'retest':'订单处理超过锁租期，新持有者已接手；请设计旧执行者的退出和释放流程，并指出方案边界。'},
 'sql-isolation':{'title':'事务隔离与观察时点','url':'https://www.postgresql.org/docs/18/transaction-iso.html',
  'reference':'PostgreSQL的Read Committed下，每条查询按语句开始时的快照读取；同一事务内两次查询之间若其他事务提交，结果可能不同。这里的规则不能直接外推为所有数据库实现。',
  'base':'PostgreSQL中事务内两次SELECT一定看到相同结果吗？请说明隔离级别与时间顺序。',
  'probe':'Read Committed事务第一次读后，另一个事务修改并提交，第二次读可能怎样变化？',
  'retest':'一个报表事务先查总数再查明细，中间有新提交；怎样解释可能的不一致，并选择合适的隔离策略？'},
 'http-idempotency':{'title':'HTTP语义与应用幂等','url':'https://www.rfc-editor.org/rfc/rfc9110.html#section-9.2.2',
  'reference':'HTTP幂等指相同请求重复执行的预期服务端效果与执行一次相同。响应和日志不必完全相同；应用自定义的幂等键及事务性去重需要自行定义和实现，不能从方法名推断支付等业务已安全。',
  'base':'客户端请求超时后重试，怎样区分HTTP幂等语义与业务去重？',
  'probe':'同一幂等键携带不同请求体时你会怎样处理，为什么不能直接返回成功？',
  'retest':'支付接口已提交数据库却丢失响应，客户端重试；请设计请求身份、结果保存与冲突处理。'},
 'java-concurrency':{'title':'Java可见性与复合操作','url':'https://docs.oracle.com/javase/specs/jls/se25/html/jls-17.html#jls-17.4.5',
  'reference':'Java内存模型用happens-before描述跨线程可见性约束；同一volatile变量的写与后续读存在相应顺序关系。一次复合的读取、计算、写入不能仅因变量是volatile就视为整体原子操作。',
  'base':'volatile计数器的count++在多线程下是否安全？请区分可见性与原子性。',
  'probe':'两个线程都读取到相同旧值，再分别加一写回，会发生什么？如何改进？',
  'retest':'限流器维护共享计数和阈值检查，为什么只给计数加volatile仍可能超限？请给出同步边界。'},
 'agent-tools':{'title':'工具参数契约与业务权限','url':'https://json-schema.org/understanding-json-schema/reference/object',
  'reference':'JSON Schema的additionalProperties:false可拒绝未声明属性。结构契约校验并不验证资源属于谁，服务端仍需按认证身份检查资源归属、工具权限、预算和最终提交；这是本项目的工程约束。',
  'base':'模型输出的工具参数通过JSON Schema后，服务端还需要检查什么？',
  'probe':'材料正文要求调用工具读取另一个用户的source_id，格式完全合法，你怎样处理？',
  'retest':'一个旧Agent任务格式合法、但用户已经修改了岗位目标；如何阻止它读新资源或提交旧结果？'}
}
ROLES={'java-backend':['java-concurrency','sql-isolation','redis-lock','http-idempotency'],
       'ai-application':['agent-tools','sql-isolation','redis-lock','http-idempotency']}


def pack(role):
    from interview_agent.question_banks.defaults import legacy_pack
    return legacy_pack(role)


def topic(scope,identity):
    if identity=='project':
        if not scope['facts']:raise ValueError('material_required_for_project')
        return {'id':'project','title':'个人贡献深挖',
                'references':[{'id':f['id'],'text':f['quote'],'origin':'user_selected_resume_statement' if f.get('resume_source') else 'user_confirmed_statement',
                               **({'resume_source':f['resume_source']} if f.get('resume_source') else {})} for f in scope['facts']],
                'project_focuses':['contribution','verification','failure','tradeoff'],
                'questions':[{'id':'project-contribution','kind':'project',
                              'text':'选择references中的fact_id；按project_focus由服务端生成问题，question_text留空。',
                              'submission':{'action':'ask_question','topic_id':'project','question_id':'project-contribution',
                                            'question_text':'','explanation':'','summary':'','project_focus':'contribution','observation':None}}],
                'submission_note':'fact_id必须取本次references[].id；若scope.answer存在，须另外填写有当前回答与引用的observation；首题observation为null。'}
    item=scope['pack']['topics'].get(identity)
    if item is None:raise ValueError('topic_not_in_pack')
    if item.get('bank_item') or item.get('generated'):
        has_reference=item['has_reference']
        return {'id':identity,'title':item['title'],'has_reference':has_reference,
                **({'bank_item':item['bank_item']} if item.get('bank_item') else {'generated':item['generated']}),
                'references':[{'id':identity+'-reference','text':item['reference'] or item['base'],'url':item['url'],
                               'origin':'bank_declared_reference' if has_reference else 'question_context_only_not_answer'}],
                'question_text_policy':'frozen_use_registered_text',
                'submission_note':'Use exact questions[].id, question_text empty and fact_id null; include current-answer observation separately. Missing probe/retest is unavailable, never invent IDs.',
                'questions':[{'id':identity+'-'+kind,'kind':kind,'text':item[kind],
                              'submission':{'action':'ask_question','topic_id':identity,'question_id':identity+'-'+kind,
                                            'question_text':'','fact_id':None,'explanation':'','summary':''}}
                             for kind in ('base','probe','retest') if item.get(kind)]}
    return {'id':identity,'title':item['title'],
            'references':[{'id':identity+'-reference','text':item['reference'],'url':item['url'],
                           'origin':'user_practice_record' if item.get('origin')=='practice_record' else 'authored_paraphrase',
                           'version':scope['pack']['version']}],
            'questions':[{'id':identity+'-'+kind,'kind':kind,'text':item[kind]} for kind in ('base','probe','retest')],
            **({'has_reference':item['has_reference']} if 'has_reference' in item else {}),
            **({'assessment':item['assessment']} if item.get('assessment') else {}),
            **({'practice':item['practice']} if item.get('practice') else {})}
