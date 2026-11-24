"""Wire contracts for the settings pane (S04A A1).

These are response shapes only. The key never appears: `key_masked` is the masked form and
`key_storage` says where it lives. `probe` returns either success fields or an error code; missing
optional fields stay missing because the route is declared with `response_model_exclude_unset`.
"""
from typing import Literal

from interview_agent.contracts.common import Contract, FalseFlag, NonNegativeInt


class Preset(Contract):
    id: str
    label: str
    provider: str
    base_url: str
    model: str


class ModelSettingsView(Contract):
    provider: str
    base_url: str
    model: str
    key_masked: str
    key_storage: str
    presets: list[Preset]
    enabled: bool
    live_authorization: str
    live_cap_cny: float
    # F07Z-165: what the currently bound row spent, its cap and its ledger state ('' = none).
    live_state: str
    live_spent_cny: float | None
    live_remaining_cny: float | None
    material_enabled: bool
    material_live_authorization: str
    material_live_cap_cny: float
    material_state: str
    material_spent_cny: float | None
    material_remaining_cny: float | None
    prep_enabled: bool
    prep_live_authorization: str
    prep_live_cap_cny: float
    prep_state: str
    prep_spent_cny: float | None
    prep_remaining_cny: float | None
    active: bool
    material_active: bool
    assessment_mode: str
    vault_available: bool


class SavedModelSettings(Contract):
    saved: Literal[True]
    key_masked: str
    key_storage: str
    enabled: bool
    live_authorization: str
    live_cap_cny: float | None
    material_enabled: bool
    material_live_authorization: str
    material_live_cap_cny: float | None
    active: bool
    material_active: bool
    prep_enabled: bool
    prep_live_authorization: str
    prep_live_cap_cny: float | None


class ProbeView(Contract):
    ok: bool
    code: str = ''
    base_url: str
    model: str
    latency_ms: NonNegativeInt | None = None
    reply_preview: str | None = None


class RemovedModelSettings(Contract):
    deleted: Literal[True]
    vault_cleared: bool
    enabled: FalseFlag
    material_enabled: FalseFlag
    key_masked: Literal['']
    active: FalseFlag
    material_active: FalseFlag
