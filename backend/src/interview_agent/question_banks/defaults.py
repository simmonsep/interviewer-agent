from interview_agent.resources import resource_root
from .contracts import BankDocument,normalized_document

DEFAULTS={
 '00000000-0000-4000-8000-000000000001':('java-backend','default-java-backend-v1.json'),
 '00000000-0000-4000-8000-000000000002':('ai-application','default-ai-application-v1.json'),
 '00000000-0000-4000-8000-000000000003':('written','default-written-v1.json'),
 # F07Z-73: aptitude items are choice-only, so this role is never auto-selected for an
 # interview pack (resolve() filters to knowledge/short_answer and no pack asks for it).
 '00000000-0000-4000-8000-000000000004':('aptitude','default-aptitude-v1.json')}


def defaults():
    from interview_agent.career.repository import digest
    result={}
    for identity,(role,filename) in DEFAULTS.items():
        doc=normalized_document(BankDocument.model_validate_json((resource_root()/'fixtures/question-banks'/filename).read_text(encoding='utf-8')))
        result[identity]={'bank_id':identity,'version':1,'origin':'builtin','enabled':True,'revision':1,
                          'document':doc,'content_sha256':digest(doc),'role':role}
    return result


def legacy_pack(role,bank=None):
    from interview_agent.career.repository import digest
    bank=bank or next(b for b in defaults().values() if b['role']==role)
    topics={i['item_id']:{'title':i['title'],'url':i['source_url'],'reference':i['reference'],
                          'base':i['question'],'probe':i['probe'],'retest':i['retest']} for i in bank['document']['items']}
    value={'role':role,'version':'ja05-role-pack-v1','review':'source_checked_draft','semantic_scoring_eligible':False,
           'topic_order':list(topics),'topics':topics}
    value['sha256']=digest(value)
    return value
