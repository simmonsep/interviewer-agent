"""Bounded source tools usable by a worker now and a scoped agent later."""
import asyncio
from datetime import datetime, timezone
import hashlib
import json
from typing import Annotated, Literal

import httpx
from pydantic import Field, model_validator
from interview_agent.contracts.common import Contract, PositiveInt
from .content import visible_text, review_flags
from .contracts import Conditions
from .sources import SourceId, SourceProbe, MAX_BYTES, USER_AGENT, endpoints, probe

CAPABILITIES = [
    {'source_id':'tme-campus','company':'腾讯音乐','search':True,'detail':True,
     'coverage':'校园门户；关键词首屏最多5条，城市在有限详情上检查；在招未知'},
    {'source_id':'xiaomi-intern','company':'小米','search':False,'detail':True,
     'coverage':'仅支持实习岗位ID详情；本步未实现该源搜索'},
]


class ResearchInput(Contract):
    expected_version: PositiveInt
    kind: Literal['search','detail','import_jd']
    source_id: SourceId = 'tme-campus'
    keyword: Annotated[str, Field(max_length=80)] = ''
    job_id: str = ''

    @model_validator(mode='after')
    def supported(self):
        if self.kind == 'search':
            if self.source_id != 'tme-campus' or not self.keyword.strip() or self.job_id:
                raise ValueError('unsupported_search')
        elif self.kind == 'detail':
            endpoints(self.source_id,self.job_id)
            if self.keyword: raise ValueError('detail_has_no_keyword')
        elif self.keyword or self.job_id:
            raise ValueError('import_uses_saved_jd')
        return self


class SearchResult(Contract):
    status: Literal['ok','access_denied','rate_limited','not_found','http_error','redirect_refused',
                    'invalid_payload','response_too_large','timeout','network_error']
    observed_at: datetime
    job_ids: Annotated[list[str], Field(max_length=5)] = []
    total: int | None = None
    response_sha256: str | None = None
    http_status: int | None = None


class ResearchBundle(Contract):
    origin: Literal['source_live','offline_fixture','user_provided']
    search: SearchResult | None = None
    probes: Annotated[list[SourceProbe], Field(max_length=5)] = []
    imported_text: str | None = None
    error_code: Literal['research_timeout'] | None = None
    executed_request: ResearchInput | None = None
    agent: 'ResearchReport | None' = None


class ResearchReport(Contract):
    action: Literal['finish','ask_user']
    question: Annotated[str, Field(max_length=300)] = ''
    read_job_ids: Annotated[list[str], Field(max_length=5)] = []
    queries: Annotated[list[ResearchInput], Field(max_length=2)] = []
    tool_log: Annotated[list[dict], Field(max_length=6)] = []
    query_observations: Annotated[list[dict], Field(max_length=2)] = []


ResearchBundle.model_rebuild()


async def search_tme(keyword: str, client: httpx.AsyncClient) -> SearchResult:
    ResearchInput(expected_version=1,kind='search',keyword=keyword)
    base = {'observed_at':datetime.now(timezone.utc)}
    body = {'page':1,'ss':5,'type':'','job_class':[],'work_city':'','setid':'','keyword':keyword.strip()}
    try:
        async with asyncio.timeout(15):
            async with client.stream('POST','https://join.tencentmusic.com/api/uc-job/list',json=body,
                                     headers={'User-Agent':USER_AGENT},follow_redirects=False) as response:
                code = response.status_code
                base['http_status'] = code
                if code != 200:
                    return SearchResult(**base,status={401:'access_denied',403:'access_denied',429:'rate_limited',404:'not_found'}.get(code,'redirect_refused' if 300<=code<400 else 'http_error'))
                raw = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=65536):
                    if len(raw)+len(chunk)>MAX_BYTES: return SearchResult(**base,status='response_too_large')
                    raw.extend(chunk)
                base['response_sha256'] = hashlib.sha256(raw).hexdigest()
                try:
                    if 'json' not in response.headers.get('content-type','').lower(): raise ValueError('json_required')
                    payload = json.loads(raw)
                    if payload['code']!='200': raise ValueError('upstream_error')
                    items = payload['data']['items']
                    if not isinstance(items,list) or len(items)>5: raise ValueError('bounded_page_required')
                    ids = []
                    for item in items:
                        identity = item['id']
                        endpoints('tme-campus',identity)
                        if identity not in ids: ids.append(identity)
                    total = payload['data'].get('_meta',{}).get('total_count')
                    if total is not None and (type(total) is not int or total<0): raise ValueError('invalid_total')
                    return SearchResult(**base,status='ok',job_ids=ids,total=total)
                except (ValueError,KeyError,TypeError,AttributeError):
                    return SearchResult(**base,status='invalid_payload')
    except (TimeoutError,httpx.TimeoutException): return SearchResult(**base,status='timeout')
    except httpx.HTTPError: return SearchResult(**base,status='network_error')


async def collect(value: ResearchInput, jd_text: str, client: httpx.AsyncClient, *, origin='source_live') -> ResearchBundle:
    if value.kind == 'import_jd':
        return ResearchBundle(origin='user_provided',imported_text=jd_text)
    search = None
    probes = []
    try:
        async with asyncio.timeout(100):
            ids = [value.job_id]
            if value.kind == 'search':
                search = await search_tme(value.keyword,client)
                if search.status != 'ok': return ResearchBundle(origin=origin,search=search)
                ids = search.job_ids
            for identity in ids:
                probes.append(await probe(value.source_id,identity,client))
    except TimeoutError:
        return ResearchBundle(origin=origin,search=search,probes=probes,error_code='research_timeout')
    return ResearchBundle(origin=origin,search=search,probes=probes)


def checked_items(value: ResearchInput, bundle: ResearchBundle, jd_text: str, conditions: dict):
    """Validate worker output again before SQL; return all successes + explicit city decisions."""
    if value.kind == 'import_jd':
        if bundle.origin!='user_provided' or bundle.imported_text!=jd_text or not jd_text.strip() or bundle.probes or bundle.search:
            raise ValueError('saved_jd_required')
        cleaned = visible_text(jd_text)
        if not cleaned: raise ValueError('empty_visible_jd')
        identity = hashlib.sha256(jd_text.encode()).hexdigest()
        return [{'source_id':'user-jd','source_job_id':identity,'source_url':None,'retrieval_url':None,
                 'observed_at':datetime.now(timezone.utc),'response_sha256':identity,'content_sha256':identity,
                 'payload':{'title':'用户提供的JD','company':None,'cities':[],'raw_text':jd_text,'visible_text':cleaned,
                            'availability':'unknown','content_warnings':review_flags(cleaned)},'city_check':'unknown','included':True}]
    if bundle.origin not in ('source_live','offline_fixture') or bundle.imported_text is not None:
        raise ValueError('source_origin_required')
    ids = [value.job_id]
    if value.kind == 'search':
        if bundle.search is None: raise ValueError('search_observation_required')
        if bundle.search.status!='ok' and bundle.probes: raise ValueError('failed_search_has_no_detail')
        ids = bundle.search.job_ids
    elif bundle.search is not None: raise ValueError('unexpected_search')
    seen = set()
    items = []
    target = Conditions.model_validate(conditions)
    normalized = {x.removesuffix('市') for x in target.cities}
    for result in bundle.probes:
        if result.source_id!=value.source_id or result.job_id not in ids or result.job_id in seen:
            raise ValueError('unexpected_or_duplicate_detail')
        seen.add(result.job_id)
        page,api = endpoints(result.source_id,result.job_id)
        if (result.source_url,result.retrieval_url)!=(page,api): raise ValueError('source_endpoint_mismatch')
        if result.status != 'detail_readable': continue
        job = result.job.model_dump(mode='json')
        canonical=json.dumps(job,ensure_ascii=False,sort_keys=True,separators=(',',':'))
        if result.content_sha256!=hashlib.sha256(canonical.encode()).hexdigest(): raise ValueError('content_hash_mismatch')
        city = ('not_requested' if not normalized else 'unknown' if not job['cities'] else
                'match' if normalized.intersection(c.removesuffix('市') for c in job['cities']) else 'conflict')
        job['content_warnings'] = review_flags(job['responsibilities']+'\n'+job['requirements'])
        items.append({'source_id':result.source_id,'source_job_id':result.job_id,'source_url':page,'retrieval_url':api,
                      'observed_at':result.observed_at,'response_sha256':result.response_sha256,
                      'content_sha256':result.content_sha256,'payload':job,'city_check':city,
                      'included':not (target.city_policy=='required' and city=='conflict')})
    # An incomplete detail batch cannot silently look like a complete success.
    if set(ids)!=seen and bundle.error_code is None: raise ValueError('missing_detail_observations')
    return items
