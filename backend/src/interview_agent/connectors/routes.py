"""Connector settings (stored next to the agent policy) and the reference-check endpoint."""
from urllib.parse import urlsplit
from fastapi import APIRouter, Request

from interview_agent.api.dependencies import PrincipalDep, errors
from interview_agent.contracts.common import Contract
from interview_agent.storage.errors import ApiError

from ..desktop.agent_policy import _path, _read, _write
from . import experience_settings, interview_experiences, public_reference, reference_check

CONNECTOR_INFO = {
    'reference-check': {
        'label': '官方来源核验',
        'description': '只对你题库自带的官方文档网址发一次 GET，用来确认链接是否还能打开；不搜索、不发送简历或回答。',
        'data_scope': '仅外发题目包中已有的 URL 与标准 HTTP 头；不含简历、回答、题库正文和账号信息。',
        'domains': list(reference_check.ALLOWED_HOSTS),
    },
    public_reference.CONNECTOR_ID: {
        'label': '准备时查阅官方文档',
        'description': '真实模型准备完整模拟时，可以查一次受支持的技术主题，并在复盘中展示实际用题引用的原文。',
        'data_scope': '仅外发宿主预设的文档地址和 HTTP 头。岗位文本、简历、回答和账号信息不发给文档站点。',
        'domains': ['docs.langchain.com'],
    },
    interview_experiences.CONNECTOR_ID: {
        'label': '准备时搜索公开面经',
        'description': '完整模拟有考点缺口时，最多搜索一次并读取两篇可访问的帖子。需配置本机搜索 Key，搜索费用由搜索服务账户结算。',
        'data_scope': '搜索只发送受支持的岗位类别、招聘类型和技术关键词。帖子只接收公开地址与标准 HTTP 头。',
        'domains': [interview_experiences.POST_HOST],
    },
}


class ConnectorSetting(Contract):
    enabled: bool


class CheckInput(Contract):
    url: str


def enabled(policy: dict, connector_id=reference_check.CONNECTOR_ID) -> bool:
    return (policy.get('connectors') or {}).get(connector_id) is True


def routes():
    router = APIRouter(prefix='/api/runtime/connectors')

    def view(request: Request, policy: dict) -> dict:
        path = _path(request)
        connectors = [{'id': key, 'enabled': enabled(policy, key), **info,
                       **({'items': reference_check.catalog()} if key == reference_check.CONNECTOR_ID else {})}
                      for key, info in CONNECTOR_INFO.items()]
        search = next(item for item in connectors if item['id'] == interview_experiences.CONNECTOR_ID)
        settings_path = experience_settings.settings_path(path)
        search['search_settings'] = experience_settings.view(settings_path)
        service = experience_settings.selected_service(settings_path)
        if service:
            search['domains'] = [urlsplit(service.endpoint).hostname, *search['domains']]
        return {'connectors': connectors, 'storage': path.name}

    @router.get('', responses=errors(401))
    async def listing(request: Request, user: PrincipalDep):
        return view(request, _read(_path(request)))

    @router.put('/{connector_id}', responses=errors(400, 401))
    async def update(connector_id: str, value: ConnectorSetting, request: Request, user: PrincipalDep):
        if connector_id not in CONNECTOR_INFO:
            raise ApiError('connector_unknown', 400)
        policy = _read(_path(request))
        connectors = dict(policy.get('connectors') or {})
        connectors[connector_id] = value.enabled
        policy['connectors'] = connectors
        _write(_path(request), policy)
        return view(request, policy)

    @router.put('/experience-search/settings', responses=errors(400, 401, 409))
    async def configure_experience_search(value: experience_settings.SearchSettingsInput, request: Request,
                                          user: PrincipalDep):
        path = _path(request)
        experience_settings.save(experience_settings.settings_path(path), value)
        return view(request, _read(path))

    @router.post('/reference-check', responses=errors(400, 401, 409))
    async def run_check(value: CheckInput, request: Request, user: PrincipalDep):
        policy = _read(_path(request))
        if not enabled(policy):
            raise ApiError('connector_disabled', 409)
        if not reference_check.allowed(value.url):
            raise ApiError('reference_url_not_allowed', 400)
        return await reference_check.check_reference(value.url)

    return router
