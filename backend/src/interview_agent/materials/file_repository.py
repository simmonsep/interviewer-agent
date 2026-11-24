"""Preview snapshots are owned, immutable and inert until an authenticated import."""
import asyncio
import hashlib
import json
import os
import subprocess
import sys
from uuid import UUID
from psycopg.types.json import Jsonb
from interview_agent.career.repository import digest
from interview_agent.storage.errors import ApiError
from .repository import MaterialRepository


def parser_command(kind):
    """How to start the parser as its own process.

    In a source checkout that is `python -m ...`; in the frozen desktop build `sys.executable` is
    the sidecar itself, so it re-enters through the `file-parser` subcommand instead - otherwise
    the child would boot the whole API and never answer.
    """
    if getattr(sys,'frozen',False):
        return [sys.executable,'file-parser',kind]
    return [sys.executable,'-X','utf8','-m','interview_agent.materials.file_parser',kind]


def parse_in_child(data,kind):
    try:
        completed = subprocess.run(parser_command(kind),
            input=data,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=8,
            env={k:os.environ[k] for k in ('SYSTEMROOT','WINDIR','PATH','TEMP','TMP') if k in os.environ},
            creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        result=json.loads(completed.stdout)
    except subprocess.TimeoutExpired:
        raise ApiError('material_file_parse_timeout',400) from None
    except (OSError,ValueError):
        raise ApiError('material_file_parser_unavailable',503) from None
    if completed.returncode or not result.get('ok'):
        raise ApiError(result.get('code','material_file_invalid'),400)
    return result['parsed']


class FileRepository:
    def __init__(self,pool):
        self.pool=pool
        self.materials=MaterialRepository(pool)

    async def _prior(self,conn,identity,key,hashed):
        row=await (await conn.execute('SELECT * FROM app.career_material_file_previews WHERE goal_id=%s AND request_id=%s', (identity,key))).fetchone()
        if row and row['request_hash']!=hashed:
            raise ApiError('idempotency_conflict',409)
        return row

    async def _check(self,conn,principal,identity,value):
        await self.materials._user(conn,principal,identity,value)
        count=await (await conn.execute('SELECT count(*) AS n FROM app.career_material_file_previews WHERE goal_id=%s',(identity,))).fetchone()
        if count['n']>=20:
            raise ApiError('material_file_preview_limit',409)

    async def preview(self,principal,identity,key,value,data):
        file_hash=hashlib.sha256(data).hexdigest()
        hashed=digest({**value.model_dump(mode='json'),'file_sha256':file_hash})
        async with self.pool.connection() as conn,conn.transaction():
            await self.materials._user(conn,principal,identity)
            old=await self._prior(conn,identity,key,hashed)
            if old:return self.view(old),False
            await self._check(conn,principal,identity,value)
        kind=value.filename.rsplit('.',1)[-1].lower()
        parsed=await asyncio.to_thread(parse_in_child,data,kind)
        # No database lock is held while the child parses. Recheck before persisting.
        async with self.pool.connection() as conn,conn.transaction():
            await self.materials._user(conn,principal,identity)
            old=await self._prior(conn,identity,key,hashed)
            if old:return self.view(old),False
            await self._check(conn,principal,identity,value)
            row=await (await conn.execute('''INSERT INTO app.career_material_file_previews
                (goal_id,goal_version,material_revision,request_id,request_hash,filename,format,file_sha256,byte_length,body,content_sha256,metadata)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *''',
                (identity,value.expected_version,value.expected_revision,key,hashed,value.filename,kind,file_hash,len(data),
                 parsed['text'],parsed['content_sha256'],Jsonb(parsed['metadata'])))).fetchone()
            return self.view(row),True

    def view(self,row):
        return {**{k:row[k] for k in ('filename','format','goal_version','material_revision','file_sha256','byte_length','content_sha256','metadata')},
                'preview_id':str(row['id']),'text':row['body'],'requires_import_confirmation':True}

    async def get(self,principal,identity,preview_id):
        async with self.pool.connection() as conn,conn.transaction():
            goal,state=await self.materials._user(conn,principal,identity)
            row=await (await conn.execute('SELECT * FROM app.career_material_file_previews WHERE id=%s AND goal_id=%s',(preview_id,identity))).fetchone()
            if not row:raise ApiError('resource_unavailable',404)
            return {**self.view(row),'stale':row['goal_version']!=goal['version'] or row['material_revision']!=state['revision'] or goal['status']=='CANCELLED'}

    async def import_preview(self,principal,identity,preview_id,key,value):
        preview_id=str(UUID(preview_id))
        hashed=digest({**value.model_dump(mode='json'),'file_preview_id':preview_id})
        async with self.pool.connection() as conn,conn.transaction():
            await self.materials._user(conn,principal,identity)
            old=await (await conn.execute('SELECT id,request_hash FROM app.career_material_sources WHERE goal_id=%s AND request_id=%s',(identity,key))).fetchone()
            if old:
                if old['request_hash']!=hashed:raise ApiError('idempotency_conflict',409)
                return str(old['id']),False
            goal,state=await self.materials._user(conn,principal,identity,value)
            row=await (await conn.execute('SELECT * FROM app.career_material_file_previews WHERE id=%s AND goal_id=%s',(preview_id,identity))).fetchone()
            if not row:raise ApiError('resource_unavailable',404)
            if row['goal_version']!=goal['version'] or row['material_revision']!=state['revision']:
                raise ApiError('material_file_preview_stale',409)
            revision=await self.materials._bump(conn,identity,state)
            source=await (await conn.execute('''INSERT INTO app.career_material_sources
                (goal_id,material_revision,request_id,request_hash,name,format,body,content_sha256,file_preview_id)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id''',
                (identity,revision,key,hashed,row['filename'],row['format'],row['body'],row['content_sha256'],preview_id))).fetchone()
            return str(source['id']),True
