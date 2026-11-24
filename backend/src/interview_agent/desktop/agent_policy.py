"""Local Agent autonomy policy and remembered approval decisions.

Modelled on the permission modes found in the open-source references we read:
goose (auto / approve / smart_approve / chat), Codex CLI (never / on-request /
on-failure / untrusted), and opencode (once / always / reject). This module is
deliberately conservative: the default 'smart_approve' never fires a background
task without a visible proposal, and 'always' grants apply only after the user
explicitly chose that option on a proposal card.

The policy file lives next to the external model configuration (or in .private
when no config path was supplied). It contains no credentials and no personal
data - just a mode string and a tool -> grant map.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from fastapi import APIRouter, Request
from interview_agent.api.dependencies import PrincipalDep, errors
from pydantic import Field

from interview_agent.contracts.common import Contract
from interview_agent.connectors.interview_experiences import CONNECTOR_ID as PREPARATION_EXPERIENCES
from interview_agent.connectors.public_reference import CONNECTOR_ID as PREPARATION_REFERENCE
from interview_agent.storage.errors import ApiError

MODES = ('auto', 'smart_approve', 'approve', 'chat')
GRANTS = ('once', 'always', 'reject')
TOOLS = (
    'read_goal', 'read_application', 'ask_user', 'prepare_job_research',
    'prepare_training', 'prepare_materials', 'prepare_application_action', 'finish',
)
LOW_RISK = frozenset({'read_goal', 'read_application', 'prepare_job_research'})
# F07Z-162: read-only connectors are a separate axis from coordinator actions; they are stored in
# the same local file so the settings page has one host-policy surface. Off unless enabled.
CONNECTORS = ('reference-check', PREPARATION_REFERENCE, PREPARATION_EXPERIENCES)

MODE_INFO = (
    {'id': 'smart_approve', 'label': '智能确认（默认）',
     'description': '只读与岗位研究一键执行；材料、训练和投递等写动作先给出确认卡。不会在你没看见时提交后台任务。'},
    {'id': 'auto', 'label': '全自动',
     'description': 'Agent 的提议直接执行，不再逐次询问。仅在明确信任本机 Agent 时选择。'},
    {'id': 'approve', 'label': '每次确认',
     'description': '所有提议都要你选一次「仅此一次 / 这类都自动 / 拒绝」。'},
    {'id': 'chat', 'label': '仅对话',
     'description': '只记录目标与对话，不执行任何后台动作。'},
)
TOOL_INFO = {tool: {'risk': 'low' if tool in LOW_RISK else 'high'} for tool in TOOLS}


class PolicyInput(Contract):
    mode: str | None = None
    grants: dict[str, str] | None = None


class DecisionInput(Contract):
    tool: str = Field(max_length=60)
    decision: str = Field(max_length=12)


class ResolveInput(Contract):
    tool: str = Field(max_length=60)


def _empty() -> dict:
    return {'version': 1, 'mode': 'smart_approve', 'grants': {}, 'connectors': {}}


def _path(request: Request) -> Path:
    return policy_path(getattr(request.app.state, 'model_config_path', None))


def policy_path(model_config=None) -> Path:
    if model_config is not None:
        return Path(model_config).parent / 'agent-policy.json'
    override = os.environ.get('INTERVIEW_AGENT_POLICY_PATH')
    return Path(override) if override else Path('.private') / 'agent-policy.json'


def _read(target: Path) -> dict:
    try:
        value = json.loads(target.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return _empty()
    if not isinstance(value, dict):
        return _empty()
    mode = value.get('mode')
    grants = value.get('grants')
    connectors = value.get('connectors')
    return {
        'version': 1,
        'mode': mode if mode in MODES else 'smart_approve',
        'grants': {k: v for k, v in grants.items() if k in TOOLS and v in ('always', 'reject')}
        if isinstance(grants, dict) else {},
        'connectors': {k: v is True for k, v in connectors.items() if k in CONNECTORS}
        if isinstance(connectors, dict) else {},
    }


def _write(target: Path, value: dict) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='agent-policy-', dir=str(target.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(value, handle, ensure_ascii=False, indent=1)
        os.replace(tmp, target)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def decide(policy: dict, tool: str) -> str:
    """Server-side authority: auto / one_click / ask / deny."""
    grant = policy['grants'].get(tool)
    if grant == 'always':
        return 'auto'
    if grant == 'reject':
        return 'deny'
    mode = policy['mode']
    if mode == 'auto':
        return 'auto'
    if mode == 'chat':
        return 'deny'
    if mode == 'approve':
        return 'ask'
    # smart_approve: low-risk work still needs one visible click, but never a
    # confirmation card; state-changing work always gets the three-way card.
    return 'one_click' if tool in LOW_RISK else 'ask'


def _validate_tool(tool: str) -> None:
    if tool not in TOOLS:
        raise ApiError('agent_policy_tool_unknown', 400)


def routes():
    router = APIRouter(prefix='/api/runtime')
    def view(request: Request, value: dict) -> dict:
        mode = request.app.state.settings.assessment_mode
        return {'mode': value['mode'], 'grants': value['grants'],
                'connectors': value.get('connectors', {}),
                'modes': list(MODE_INFO), 'tools': TOOL_INFO,
                'effects': {tool: decide(value, tool) for tool in TOOLS},
                'storage': _path(request).name,
                'assessment_mode': mode,
                # F07Z-61: this sentence used to claim no paid calls unconditionally, which is
                # false as soon as the API is started with --assessment-mode live.
                'note': ('策略与授权只保存在本机；当前是脚本演示模式，不调用付费模型。'
                         if mode == 'scripted' else
                         '策略与授权只保存在本机；当前是真实模型模式，调用会产生费用，按实际用量记账。')}

    @router.get('/agent-policy', responses=errors(401))
    async def show(request: Request, user:PrincipalDep):
        return view(request, _read(_path(request)))

    @router.put('/agent-policy')
    async def update(request: Request, value: PolicyInput, user:PrincipalDep):
        current = _read(_path(request))
        if value.mode is not None:
            if value.mode not in MODES:
                raise ApiError('agent_policy_mode_invalid', 400)
            current['mode'] = value.mode
        if value.grants is not None:
            # Providing the grants object replaces the whole remembered set;
            # omitting it (e.g. when only changing mode) leaves it untouched.
            if len(value.grants) > len(TOOLS):
                raise ApiError('agent_policy_grants_invalid', 400)
            for tool, grant in value.grants.items():
                _validate_tool(tool)
                if grant not in GRANTS:
                    raise ApiError('agent_policy_grant_invalid', 400)
            current['grants'] = {tool: grant for tool, grant in value.grants.items()
                                 if grant in ('always', 'reject')}
        _write(_path(request), current)
        return view(request, current)

    @router.post('/agent-policy/decision')
    async def record(request: Request, value: DecisionInput, user:PrincipalDep):
        _validate_tool(value.tool)
        if value.decision not in GRANTS:
            raise ApiError('agent_policy_grant_invalid', 400)
        current = _read(_path(request))
        if value.decision == 'once':
            current['grants'].pop(value.tool, None)
        else:
            current['grants'][value.tool] = value.decision
        _write(_path(request), current)
        return view(request, current)

    @router.post('/agent-policy/resolve')
    async def resolve(request: Request, value: ResolveInput, user:PrincipalDep):
        _validate_tool(value.tool)
        current = _read(_path(request))
        return {'tool': value.tool, 'effect': decide(current, value.tool),
                'mode': current['mode'], 'risk': TOOL_INFO[value.tool]['risk'],
                'grant': current['grants'].get(value.tool)}

    return router
