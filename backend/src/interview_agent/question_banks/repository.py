from uuid import UUID,uuid5,NAMESPACE_URL
from psycopg.types.json import Jsonb
from interview_agent.storage.auth import require_session
from interview_agent.storage.errors import ApiError
from interview_agent.career.repository import digest
from .defaults import defaults,legacy_pack
from .contracts import normalized_document


def summary(bank):
    return {k:bank[k] for k in ('bank_id','version','origin','enabled','revision','content_sha256')}|{
        'name':bank['document']['name'],'item_count':len(bank['document']['items'])}


class BankRepository:
    def __init__(self,pool):self.pool=pool

    async def _auth(self,conn,user,lock=False):
        await require_session(conn,user)
        if lock:await conn.execute('SELECT id FROM auth.users WHERE id=%s FOR UPDATE',(user.user_id,))

    async def preview(self,user,key,value):
        document=normalized_document(value.document);hashed=digest(document)
        async with self.pool.connection() as conn,conn.transaction():
            await self._auth(conn,user,True)
            row=await (await conn.execute('SELECT * FROM app.question_bank_imports WHERE user_id=%s AND request_id=%s',(user.user_id,key))).fetchone()
            if row:
                if row['request_hash']!=hashed:raise ApiError('idempotency_conflict',409)
                return self.preview_view(row),False
            count=await (await conn.execute('SELECT count(*) AS n FROM app.question_bank_imports WHERE user_id=%s',(user.user_id,))).fetchone()
            if count['n']>=100:raise ApiError('question_bank_preview_limit',409)
            row=await (await conn.execute('''INSERT INTO app.question_bank_imports(user_id,request_id,request_hash,document,content_sha256)
                VALUES(%s,%s,%s,%s,%s) RETURNING *''',(user.user_id,key,hashed,Jsonb(document),hashed))).fetchone()
            return self.preview_view(row),True

    def preview_view(self,row):
        return {'preview_id':str(row['id']),'document':row['document'],'content_sha256':row['content_sha256'],
                'requires_confirmation':True,'items_without_reference':[i['item_id'] for i in row['document']['items'] if not i['reference']],
                'choice_items_without_key':[i['item_id'] for i in row['document']['items'] if i['kind'] in ('single_choice','multiple_choice') and not i['correct_option_ids']],
                'programming_items_require_validation':[i['item_id'] for i in row['document']['items'] if i['kind']=='java_programming'],
                'review':'user_content_not_verified','scope':'declared_question_resources_not_execution_permission'}

    async def get_preview(self,user,identity):
        async with self.pool.connection() as conn,conn.transaction():
            await self._auth(conn,user)
            row=await (await conn.execute('SELECT * FROM app.question_bank_imports WHERE id=%s AND user_id=%s',(identity,user.user_id))).fetchone()
            if not row:raise ApiError('resource_unavailable',404)
            return self.preview_view(row)

    async def commit(self,user,preview_id,key,value):
        preview_id=str(UUID(preview_id));hashed=digest({'preview_id':preview_id,**value.model_dump(mode='json')})
        async with self.pool.connection() as conn,conn.transaction():
            await self._auth(conn,user,True)
            old=await (await conn.execute('SELECT bank_id,version,request_hash FROM app.question_bank_versions WHERE user_id=%s AND request_id=%s',(user.user_id,key))).fetchone()
            if old:
                if old['request_hash']!=hashed:raise ApiError('idempotency_conflict',409)
                return {'bank_id':str(old['bank_id']),'version':old['version']},False
            p=await (await conn.execute('SELECT * FROM app.question_bank_imports WHERE id=%s AND user_id=%s',(preview_id,user.user_id))).fetchone()
            if not p:raise ApiError('resource_unavailable',404)
            used=await (await conn.execute('SELECT 1 FROM app.question_bank_versions WHERE preview_id=%s',(preview_id,))).fetchone()
            if used:raise ApiError('question_bank_preview_already_committed',409)
            if value.bank_id:
                if value.bank_id in defaults():raise ApiError('builtin_question_bank_read_only',403)
                bank=await (await conn.execute('SELECT * FROM app.question_banks WHERE id=%s AND user_id=%s FOR UPDATE',(value.bank_id,user.user_id))).fetchone()
                if not bank:raise ApiError('resource_unavailable',404)
                if bank['current_version']!=value.expected_version:raise ApiError('question_bank_version_conflict',409)
                if bank['current_version']>=30:raise ApiError('question_bank_version_limit',409)
                version=bank['current_version']+1
                await conn.execute('UPDATE app.question_banks SET current_version=%s,revision=revision+1 WHERE id=%s',(version,bank['id']))
            else:
                count=await (await conn.execute('SELECT count(*) AS n FROM app.question_banks WHERE user_id=%s',(user.user_id,))).fetchone()
                if count['n']>=20:raise ApiError('question_bank_limit',409)
                bank=await (await conn.execute('INSERT INTO app.question_banks(user_id,current_version) VALUES(%s,1) RETURNING *',(user.user_id,))).fetchone()
                version=1
            await conn.execute('''INSERT INTO app.question_bank_versions(bank_id,user_id,version,preview_id,document,content_sha256,request_id,request_hash)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s)''',(bank['id'],user.user_id,version,preview_id,Jsonb(p['document']),p['content_sha256'],key,hashed))
            return {'bank_id':str(bank['id']),'version':version},True

    async def _version(self,conn,user_id,identity,version,for_training=False):
        identity=str(UUID(str(identity)));builtins=defaults()
        if identity in builtins:
            if version!=1:raise ApiError('resource_unavailable',404)
            return builtins[identity]
        row=await (await conn.execute('''SELECT b.id,b.revision,b.enabled,v.version,v.document,v.content_sha256
            FROM app.question_banks b JOIN app.question_bank_versions v ON v.bank_id=b.id
            WHERE b.id=%s AND b.user_id=%s AND v.version=%s FOR SHARE OF b''',(identity,user_id,version))).fetchone()
        if not row:raise ApiError('resource_unavailable',404)
        if for_training and not row['enabled']:raise ApiError('question_bank_disabled',409)
        return {'bank_id':str(row['id']),'origin':'private',**{k:row[k] for k in ('revision','enabled','version','document','content_sha256')}}

    async def listing(self,user):
        async with self.pool.connection() as conn,conn.transaction():
            await self._auth(conn,user)
            rows=await (await conn.execute('SELECT id,current_version FROM app.question_banks WHERE user_id=%s ORDER BY id',(user.user_id,))).fetchall()
            banks=[summary(b) for b in defaults().values()]
            for row in rows:banks.append(summary(await self._version(conn,user.user_id,row['id'],row['current_version'])))
            return {'banks':banks}

    async def export(self,user,identity,version):
        async with self.pool.connection() as conn,conn.transaction():
            await self._auth(conn,user)
            return await self._version(conn,user.user_id,identity,version)

    async def set_enabled(self,user,identity,value):
        identity=str(UUID(identity))
        async with self.pool.connection() as conn,conn.transaction():
            await self._auth(conn,user,True)
            if identity in defaults():raise ApiError('builtin_question_bank_read_only',403)
            row=await (await conn.execute('SELECT * FROM app.question_banks WHERE id=%s AND user_id=%s FOR UPDATE',(identity,user.user_id))).fetchone()
            if not row:raise ApiError('resource_unavailable',404)
            if row['revision']!=value.expected_revision:raise ApiError('question_bank_revision_conflict',409)
            await conn.execute('UPDATE app.question_banks SET enabled=%s,revision=revision+1 WHERE id=%s',(value.enabled,identity))

    async def resolve(self,conn,user_id,role,refs):
        if not refs:
            bank=next(b for b in defaults().values() if b['role']==role)
            return legacy_pack(role,bank),[summary(bank)]
        banks=[await self._version(conn,user_id,r.bank_id,r.version,True) for r in sorted(refs,key=lambda r:r.bank_id)]
        topics={}
        # Restore the user's explicit order after acquiring shared row locks in stable order.
        banks.sort(key=lambda b:next(i for i,r in enumerate(refs) if r.bank_id==b['bank_id']))
        for bank in banks:
            items=[i for i in bank['document']['items'] if role in i['roles'] and i['kind'] in ('knowledge','short_answer')]
            if not items:raise ApiError('question_bank_role_not_supported',400)
            for item in items:
                tid='b-'+uuid5(NAMESPACE_URL,bank['bank_id']+':'+str(bank['version'])+':'+item['item_id']).hex
                provenance={**summary(bank),'item_id':item['item_id'],'item_sha256':digest(item)}
                topics[tid]={'title':item['title'],'url':item['source_url'],'reference':item['reference'],'base':item['question'],
                             'probe':item['probe'],'retest':item['retest'],'has_reference':bool(item['reference']),'bank_item':provenance}
        fingerprint=digest([{k:b[k] for k in ('bank_id','version','content_sha256')} for b in banks])
        value={'role':role,'version':'bank-selection-v1-'+fingerprint[:16],'review':'declared_not_verified','semantic_scoring_eligible':False,
               'topic_order':list(topics),'topics':topics}
        value['sha256']=digest(value)
        return value,[summary(b) for b in banks]
