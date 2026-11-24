"""Runtime configuration the user controls from the desktop settings pane.

The model-access file stays in the external configuration directory, next to
api.json/worker.json. Saving here only persists the user's choice; it never
switches the provider on by itself - live mode still requires the external
development-mode configuration and a Worker restart, so the free scripted
default cannot be bypassed from the UI.

The API key is stored in the Windows Credential Manager when available (see
desktop/key_vault.py), never as plaintext in model.json. On other platforms or
if the vault refuses the write, the UI is told that the local file is used.
GET never returns the secret, only a mask.
"""

import re
from pathlib import Path

from fastapi import APIRouter, Request
from pydantic import Field

from interview_agent.contracts.common import Contract
from interview_agent.desktop import key_vault
from interview_agent.storage.errors import ApiError
from .connectivity import ProbeError, probe
from .contracts import (ModelSettingsView, ProbeView, RemovedModelSettings,
                        SavedModelSettings)
from interview_agent.api.dependencies import PrincipalDep

# Filling aids only. The repository has never contacted these endpoints and does
# not claim they work; the user still supplies their own key. No model is called.
PRESETS = (
    {'id': 'deepseek', 'label': 'DeepSeek', 'provider': 'deepseek',
     'base_url': 'https://api.deepseek.com', 'model': 'deepseek-chat'},
    {'id': 'openrouter', 'label': 'OpenRouter', 'provider': 'openrouter',
     'base_url': 'https://openrouter.ai/api/v1', 'model': 'openai/gpt-4o-mini'},
    {'id': 'self-hosted', 'label': '自建 / 兼容网关', 'provider': '', 'base_url': '', 'model': ''},
)
VAULT = 'windows_credential_manager'
FILE = 'local_file'


class ModelProbeInput(Contract):
    """Optional overrides so the user can test before saving."""
    base_url: str = Field(default='', max_length=300)
    model: str = Field(default='', max_length=120)
    api_key: str = Field(default='', max_length=400)


class ModelSettingsInput(Contract):
    provider: str = Field(default='', max_length=60)
    base_url: str = Field(default='', max_length=300)
    model: str = Field(default='', max_length=120)
    api_key: str = Field(default='', max_length=400)
    enabled: bool = False
    # F07Z-62: the explicit spending cap for desktop live mode. The table and the policy both
    # cap at 2 CNY; this is the user's own, lower choice.
    live_cap_cny: float = Field(default=1.0, ge=0.2, le=2.0)
    # F07Z-112: materials are a separate capability with their own policy (ja04) and cap.
    material_enabled: bool = False
    material_live_cap_cny: float = Field(default=0.5, ge=0.2, le=2.0)
    # S03c3: the planning Agent is the third capability; it has its own switch and cap so it can
    # never ride on the interview flag.
    prep_enabled: bool = False
    prep_live_cap_cny: float = Field(default=0.2, ge=0.2, le=2.0)
    # F07Z-165 (arbitration THIRD_OPTION): an explicit "enable live" is one authorization
    # instance. The client sends a request id it generated; the row id carries it, so retrying
    # the same request is idempotent while a new request gets a fresh cap. Callers that send
    # neither field keep the old deterministic id (compatibility for older model.json files).
    authorization_request_id: str = Field(default='', max_length=64)
    new_authorization: bool = False
    material_authorization_request_id: str = Field(default='', max_length=64)
    material_new_authorization: bool = False
    prep_authorization_request_id: str = Field(default='', max_length=64)
    prep_new_authorization: bool = False

    def normalized(self):
        provider = self.provider.strip()
        model = self.model.strip()
        base_url = self.base_url.strip()
        api_key = self.api_key.strip()
        if base_url and not base_url.startswith('https://'):
            raise ApiError('model_base_url_requires_https', 400)
        if api_key and any(character.isspace() for character in api_key):
            raise ApiError('model_key_whitespace', 400)
        return {'provider': provider, 'base_url': base_url, 'model': model,
                'api_key': api_key, 'enabled': bool(self.enabled),
                'material_enabled': bool(self.material_enabled),
                'prep_enabled': bool(self.prep_enabled),
                'live_cap_nano': int(round(float(self.live_cap_cny) * 1_000_000_000)),
                'material_live_cap_nano': int(round(float(self.material_live_cap_cny) * 1_000_000_000)),
                'prep_live_cap_nano': int(round(float(self.prep_live_cap_cny) * 1_000_000_000))}


def _mask(key: str) -> str:
    if not key:
        return ''
    return f'{key[:3]}…{key[-4:]}' if len(key) > 8 else '…'


_REQUEST_ID = re.compile(r'[0-9a-zA-Z][0-9a-zA-Z._-]{7,63}')


def _authorization_request_id(raw: str) -> str:
    """Return the client-generated instance id that names this authorization row.

    It becomes part of a text primary key shared with the audit trail, so keep it short,
    printable and free of anything that could make two rows look alike.
    """
    value = (raw or '').strip()
    if not _REQUEST_ID.fullmatch(value):
        raise ApiError('authorization_request_id_invalid', 400)
    return value


def routes():
    router = APIRouter(prefix='/api/runtime')

    async def principal(request: Request):
        result = await request.app.state.auth.resolve(request.cookies.get(request.app.state.settings.cookie_name))
        if result is None:
            raise ApiError('authentication_required', 401)
        return result[0]

    def path(request: Request) -> Path:
        target = getattr(request.app.state, 'model_config_path', None)
        if target is None:
            raise ApiError('model_config_path_not_set', 404)
        return Path(target)

    def read(target: Path) -> dict:
        import json
        try:
            value = json.loads(target.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            value = {}
        if not isinstance(value, dict):
            value = {}
        result = {key: value.get(key, '') for key in ('provider', 'base_url', 'model', 'api_key', 'enabled',
                                                      'live_authorization', 'live_cap_nano',
                                                      'material_enabled', 'material_live_authorization',
                                                      'material_live_cap_nano',
                                                      'prep_enabled', 'prep_live_authorization',
                                                      'prep_live_cap_nano')}
        result['key_storage'] = value.get('key_storage', '')
        return result

    def effective_key(value: dict) -> str:
        if value.get('key_storage') == VAULT:
            return key_vault.load() or ''
        return str(value.get('api_key') or '') or (key_vault.load() or '')

    async def ensure_authorization(request: Request, identity: str, digest: str, cap_nano: int):
        """Create this authorization row, or accept an identical retry of the same request.

        A retry must not change the row: same id plus same policy/cap is a no-op, anything else
        is a conflict, so a request id can never quietly rewrite an older budget.
        """
        async with request.app.state.pool.connection() as conn, conn.transaction():
            row = await (await conn.execute(
                'SELECT policy_hash,max_nano FROM app.model_authorizations WHERE id=%s',
                (identity,))).fetchone()
            if row is not None and (row['policy_hash'] != digest or row['max_nano'] != cap_nano):
                raise ApiError('authorization_conflict', 409)
            await conn.execute(
                'INSERT INTO app.model_authorizations(id,policy_hash,max_nano) VALUES(%s,%s,%s) '
                'ON CONFLICT (id) DO NOTHING', (identity, digest, cap_nano))

    async def bound_authorization(request: Request, identity: str, digest: str, cap_nano: int):
        """A plain save keeps the row already bound to this capability; it never mints budget."""
        if not identity:
            raise ApiError('authorization_requires_new_instance', 409)
        async with request.app.state.pool.connection() as conn:
            row = await (await conn.execute(
                'SELECT policy_hash,max_nano FROM app.model_authorizations WHERE id=%s',
                (identity,))).fetchone()
        if row is None or row['policy_hash'] != digest or row['max_nano'] != cap_nano:
            raise ApiError('authorization_requires_new_instance', 409)
        return identity

    async def usage(request: Request, identity: str, cap_nano: int):
        """Read-only aggregate for the pane. Same rule the ledger uses: a settled call counts
        what it cost, everything else counts its reservation; state comes from the row itself.
        """
        if not identity:
            return '', None, None
        async with request.app.state.pool.connection() as conn:
            state_row = await (await conn.execute(
                'SELECT state FROM app.model_authorizations WHERE id=%s',
                (identity,))).fetchone()
            row = await (await conn.execute(
                "SELECT coalesce(sum(CASE WHEN state='KNOWN' THEN accounted_nano "
                "ELSE reserved_nano END),0) AS used FROM app.model_http_calls "
                'WHERE authorization_id=%s', (identity,))).fetchone()
        spent = float(row['used']) / 1_000_000_000 if row else 0.0
        remaining = max(0.0, cap_nano / 1_000_000_000 - spent) if cap_nano else None
        return (str(state_row['state']) if state_row else ''), spent, remaining

    @router.get('/model-settings', response_model=ModelSettingsView)
    async def show(request: Request, user: PrincipalDep):
        value = read(path(request))
        settings = request.app.state.settings
        key = effective_key(value)
        storage = VAULT if value.get('key_storage') == VAULT else (FILE if key else '')
        material_authorization = str(value.get('material_live_authorization') or '')
        live_authorization = str(value.get('live_authorization') or '')
        prep_authorization = str(value.get('prep_live_authorization') or '')

        def cap_of(key: str) -> int:
            return int(value[key]) if str(value.get(key) or '').isdigit() else 0

        # F07Z-120/F07Z-165: the pane shows what the bound authorization already spent, next to
        # its cap and state. Read-only aggregate; a PAUSED row is visible here instead of only
        # showing up as a failed run.
        live_state, live_spent, live_remaining = await usage(
            request, live_authorization, cap_of('live_cap_nano'))
        material_state, material_spent, material_remaining = await usage(
            request, material_authorization, cap_of('material_live_cap_nano'))
        prep_state, prep_spent, prep_remaining = await usage(
            request, prep_authorization, cap_of('prep_live_cap_nano'))
        return {'provider': value['provider'], 'base_url': value['base_url'], 'model': value['model'],
                'key_masked': _mask(key), 'key_storage': storage, 'presets': list(PRESETS),
                'enabled': bool(value['enabled']),
                # F07Z-62: the desktop host reads these two non-secret fields at startup and
                # turns the API/workers live only when `enabled` is true.
                'live_authorization': live_authorization,
                'live_cap_cny': (int(value['live_cap_nano']) / 1_000_000_000
                                 if str(value.get('live_cap_nano') or '').isdigit() else 1.0),
                # F07Z-165: state/spent/remaining for this exact row. `''` means nothing is
                # bound yet; PAUSED means the next run needs a new authorization.
                'live_state': live_state,
                'live_spent_cny': live_spent,
                'live_remaining_cny': live_remaining,
                # F07Z-112: the materials capability has its own flag, cap and authorization.
                'material_enabled': bool(value.get('material_enabled')),
                'material_live_authorization': material_authorization,
                'material_live_cap_cny': (int(value['material_live_cap_nano']) / 1_000_000_000
                                          if str(value.get('material_live_cap_nano') or '').isdigit() else 0.5),
                # F07Z-120: spent/remaining for this exact authorization row.
                'material_state': material_state,
                'material_spent_cny': material_spent,
                'material_remaining_cny': material_remaining,
                # Truthful status: saving is not activation. Live needs a desktop restart.
                'prep_enabled': bool(value.get('prep_enabled')),
                'prep_live_authorization': prep_authorization,
                'prep_live_cap_cny': (int(value['prep_live_cap_nano']) / 1_000_000_000
                                      if str(value.get('prep_live_cap_nano') or '').isdigit() else 0.2),
                'prep_state': prep_state,
                'prep_spent_cny': prep_spent,
                'prep_remaining_cny': prep_remaining,
                'active': bool(value['enabled']) and settings.assessment_mode == 'live',
                'material_active': bool(value.get('material_enabled')) and settings.material_live,
                'assessment_mode': settings.assessment_mode,
                'vault_available': key_vault.available()}

    @router.post('/model-settings/test', response_model=ProbeView,
                 response_model_exclude_unset=True)
    async def test_connection(request: Request, user: PrincipalDep,
                              value: ModelProbeInput = ModelProbeInput()):
        """One real chat completion against the configured endpoint.

        Returns ok=false with a stable code instead of raising, so the settings pane can
        explain a rejected key or an unreachable host without the request looking broken.
        The key is never echoed back.
        """
        stored = read(path(request))
        base_url = (value.base_url or stored['base_url']).strip()
        model = (value.model or stored['model']).strip()
        key = (value.api_key or effective_key(stored)).strip()
        try:
            result = await probe(base_url=base_url, model=model, key=key)
        except ProbeError as error:
            return {'ok': False, 'code': error.code, 'base_url': base_url, 'model': model}
        return {**result, 'ok': True}

    @router.put('/model-settings', response_model=SavedModelSettings)
    async def update(request: Request, value: ModelSettingsInput, user: PrincipalDep):
        import json
        target = path(request)
        stored = read(target)
        merged = value.normalized()
        incoming = merged['api_key']
        existing = effective_key(stored)
        final_key = incoming or existing
        if (merged['enabled'] or merged['material_enabled'] or merged['prep_enabled']) and (not merged['provider'] or not merged['base_url']
                                  or not merged['model'] or not final_key):
            raise ApiError('model_settings_incomplete', 400)

        if incoming:
            if key_vault.store(incoming):
                storage = VAULT
                merged['api_key'] = ''
            else:
                storage = FILE
                merged['api_key'] = incoming
                key_vault.clear()
        else:
            storage = VAULT if stored.get('key_storage') == VAULT and key_vault.load() else FILE
            merged['api_key'] = '' if storage == VAULT else existing
        merged['key_storage'] = storage
        fields = value.model_fields_set
        interview_explicit = 'new_authorization' in fields or 'authorization_request_id' in fields
        material_explicit = ('material_new_authorization' in fields
                             or 'material_authorization_request_id' in fields)
        prep_explicit = ('prep_new_authorization' in fields
                         or 'prep_authorization_request_id' in fields)
        if merged['enabled']:
            # F07Z-62 / F07Z-165 (arbitration THIRD_OPTION): one explicit enable is one
            # authorization instance. A plain save re-binds the row already in model.json and
            # never mints budget; a changed model or cap needs an explicit new request. Callers
            # that send neither new field keep the old deterministic id, so an older model.json
            # keeps working exactly as before.
            from interview_agent.billing.config import default_policy
            policy = default_policy().with_model_settings({'base_url': merged['base_url'],
                                                           'model': merged['model']})
            cap_nano = int(merged.pop('live_cap_nano'))
            if not interview_explicit:
                authorization = f'desktop-live-{policy.digest[:10]}-{cap_nano // 1_000_000}'
                await ensure_authorization(request, authorization, policy.digest, cap_nano)
            elif value.new_authorization:
                authorization = 'desktop-live-' + _authorization_request_id(
                    value.authorization_request_id)
                await ensure_authorization(request, authorization, policy.digest, cap_nano)
            else:
                authorization = await bound_authorization(
                    request, str(stored.get('live_authorization') or ''),
                    policy.digest, cap_nano)
            merged['live_authorization'] = authorization
            merged['live_cap_nano'] = cap_nano
        else:
            merged.pop('live_cap_nano', None)
            merged['live_authorization'] = str(stored.get('live_authorization') or '')
            merged['live_cap_nano'] = stored.get('live_cap_nano', '')
        if merged['material_enabled']:
            # F07Z-112: materials use their own policy (ja04) and therefore their own
            # authorization instance, created by the same rules as the interview one.
            from interview_agent.materials.billing import default_material_policy
            material_policy = default_material_policy().with_model_settings(
                {'base_url': merged['base_url'], 'model': merged['model']})
            material_cap_nano = int(merged.pop('material_live_cap_nano'))
            if not material_explicit:
                material_authorization = (f'desktop-material-live-{material_policy.digest[:10]}-'
                                          f'{material_cap_nano // 1_000_000}')
                await ensure_authorization(request, material_authorization,
                                           material_policy.digest, material_cap_nano)
            elif value.material_new_authorization:
                material_authorization = ('desktop-material-live-'
                                          + _authorization_request_id(
                                              value.material_authorization_request_id))
                await ensure_authorization(request, material_authorization,
                                           material_policy.digest, material_cap_nano)
            else:
                material_authorization = await bound_authorization(
                    request, str(stored.get('material_live_authorization') or ''),
                    material_policy.digest, material_cap_nano)
            merged['material_live_authorization'] = material_authorization
            merged['material_live_cap_nano'] = material_cap_nano
        else:
            merged.pop('material_live_cap_nano', None)
            merged['material_live_authorization'] = str(stored.get('material_live_authorization') or '')
            merged['material_live_cap_nano'] = stored.get('material_live_cap_nano', '')
        if merged['prep_enabled']:
            # S03c3: the planning capability has its own authorization instance. Same policy
            # family as the interview role, separate id so its spend stays attributable.
            from interview_agent.billing.config import default_policy
            prep_policy = default_policy().with_model_settings({'base_url': merged['base_url'],
                                                                'model': merged['model']})
            prep_cap_nano = int(merged.pop('prep_live_cap_nano'))
            if not prep_explicit:
                prep_authorization = (f'desktop-prep-live-{prep_policy.digest[:10]}-'
                                      f'{prep_cap_nano // 1_000_000}')
                await ensure_authorization(request, prep_authorization,
                                           prep_policy.digest, prep_cap_nano)
            elif value.prep_new_authorization:
                prep_authorization = ('desktop-prep-live-'
                                      + _authorization_request_id(
                                          value.prep_authorization_request_id))
                await ensure_authorization(request, prep_authorization,
                                           prep_policy.digest, prep_cap_nano)
            else:
                prep_authorization = await bound_authorization(
                    request, str(stored.get('prep_live_authorization') or ''),
                    prep_policy.digest, prep_cap_nano)
            merged['prep_live_authorization'] = prep_authorization
            merged['prep_live_cap_nano'] = prep_cap_nano
        else:
            merged.pop('prep_live_cap_nano', None)
            merged['prep_live_authorization'] = str(stored.get('prep_live_authorization') or '')
            merged['prep_live_cap_nano'] = stored.get('prep_live_cap_nano', '')
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(merged, ensure_ascii=False, indent=1), encoding='utf-8')
        return {'saved': True, 'key_masked': _mask(final_key), 'key_storage': storage,
                'enabled': merged['enabled'], 'live_authorization': merged['live_authorization'],
                'live_cap_cny': (int(merged['live_cap_nano']) / 1_000_000_000
                                 if str(merged.get('live_cap_nano') or '').isdigit() else None),
                'material_enabled': merged['material_enabled'],
                'material_live_authorization': merged['material_live_authorization'],
                'material_live_cap_cny': (int(merged['material_live_cap_nano']) / 1_000_000_000
                                          if str(merged.get('material_live_cap_nano') or '').isdigit() else None),
                'active': merged['enabled'] and request.app.state.settings.assessment_mode == 'live',
                'material_active': merged['material_enabled'] and request.app.state.settings.material_live,
                'prep_enabled': merged['prep_enabled'],
                'prep_live_authorization': merged['prep_live_authorization'],
                'prep_live_cap_cny': (int(merged['prep_live_cap_nano']) / 1_000_000_000
                                      if str(merged.get('prep_live_cap_nano') or '').isdigit() else None)}

    @router.delete('/model-settings', response_model=RemovedModelSettings)
    async def remove_key(request: Request, user: PrincipalDep):
        """Remove the saved model key and switch live off.

        Replacement alone leaves users stuck with a key they no longer trust; this clears the
        credential manager entry and the fallback file field, and drops the desktop
        authorization reference so a restart cannot go live. The authorization row itself is
        kept for the audit trail, like every other model_authorizations record.
        """
        import json
        target = path(request)
        stored = read(target)
        removed = key_vault.clear()
        stored['api_key'] = ''
        stored['key_storage'] = ''
        stored['enabled'] = False
        stored['live_authorization'] = ''
        stored['live_cap_nano'] = ''
        stored['material_enabled'] = False
        stored['material_live_authorization'] = ''
        stored['material_live_cap_nano'] = ''
        # S03c3b: planning is the third capability. It was added to PUT/GET after this handler
        # was written, so removing the key used to leave it armed: the host would still start
        # `--provider live` and every preparation failed on missing credentials instead of
        # falling back to the free plan (0 cost, but a wrong, confusing failure).
        stored['prep_enabled'] = False
        stored['prep_live_authorization'] = ''
        stored['prep_live_cap_nano'] = ''
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(stored, ensure_ascii=False, indent=1), encoding='utf-8')
        return {'deleted': True, 'vault_cleared': bool(removed), 'enabled': False,
                'material_enabled': False, 'key_masked': '', 'active': False, 'material_active': False}

    return router
