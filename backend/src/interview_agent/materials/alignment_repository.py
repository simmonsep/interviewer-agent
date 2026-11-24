import hashlib
from uuid import UUID
from psycopg.types.json import Jsonb
from interview_agent.career.repository import digest
from interview_agent.storage.errors import ApiError
from .contracts import validate_result
from .repository import MaterialRepository


async def current_alignment(conn,goal,state,target,run_id):
    run=await (await conn.execute('SELECT * FROM app.career_material_runs WHERE id=%s AND goal_id=%s',(run_id,goal['id']))).fetchone()
    if not run:raise ApiError('resource_unavailable',404)
    if (run['status']!='SUCCEEDED' or run['goal_version']!=goal['version'] or run['material_revision']!=state['revision']
        or digest(run['input_payload']['target'])!=digest(target)):
        raise ApiError('material_alignment_stale',409)
    result=validate_result(run['input_payload'],run['result'])
    if result.action!='alignment':raise ApiError('material_alignment_required',400)
    return {'run_id':str(run['id']),'result_sha256':digest(result.model_dump(mode='json')),
            'requirements':result.alignment_items,'coverage':'reviewed_requirements_only_not_exhaustive',
            'verification':'model_assessment_of_user_declarations_not_ability_certification'}


class AlignmentRepository:
    def __init__(self,pool):self.pool,self.materials=pool,MaterialRepository(pool)

    async def clarify(self,principal,gid,run_id,key,value):
        run_id=str(UUID(run_id));hashed=digest({**value.model_dump(mode='json'),'clarification_run_id':run_id})
        async with self.pool.connection() as conn,conn.transaction():
            await self.materials._user(conn,principal,gid)
            old=await (await conn.execute('SELECT id,request_hash FROM app.career_material_sources WHERE goal_id=%s AND request_id=%s',(gid,key))).fetchone()
            if old:
                if old['request_hash']!=hashed:raise ApiError('idempotency_conflict',409)
                return str(old['id']),False
            goal,state=await self.materials._user(conn,principal,gid,value)
            alignment=await current_alignment(conn,goal,state,await self.materials._target(conn,goal),run_id)
            parts,answers,size=[],[],0
            for answer in value.answers:
                if answer.requirement_index>=len(alignment['requirements']):raise ApiError('clarification_not_requested',400)
                item=alignment['requirements'][answer.requirement_index]
                if item['evidence_status']!='needs_clarification':raise ApiError('clarification_not_requested',400)
                start=size+(2 if parts else 0);size=start+len(answer.text)
                parts.append(answer.text)
                answers.append({'requirement_index':answer.requirement_index,'requirement_quote':item['requirement_quote'],
                                'question':item['question'],'start_char':start,'end_char':size})
            text='\n\n'.join(parts)
            revision=await self.materials._bump(conn,gid,state)
            context={**alignment,'answers':answers,'qualification':'unconfirmed_user_answers'}
            row=await (await conn.execute('''INSERT INTO app.career_material_sources
                (goal_id,material_revision,request_id,request_hash,name,format,body,content_sha256,clarification_context)
                VALUES(%s,%s,%s,%s,'经历澄清.txt','text',%s,%s,%s) RETURNING id''',
                (gid,revision,key,hashed,text,hashlib.sha256(text.encode()).hexdigest(),Jsonb(context)))).fetchone()
            return str(row['id']),True
