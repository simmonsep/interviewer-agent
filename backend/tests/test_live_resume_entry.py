import argparse
from pathlib import Path
import runpy
import pytest


def entry():return runpy.run_path(str(Path(__file__).resolve().parents[2]/'scripts/check-live-resume.py'))


@pytest.mark.parametrize('patch,code',[
    ({'stage':'invalid'},'unsupported_stage'),
    ({'mode':'live'},'live_requires_explicit_stage_approval_flags'),
    ({'mode':'live','execute_live':True,'max_cny':'2','credentials':Path('unused')},'live_requires_explicit_stage_approval_flags'),
    ({'credentials':Path('must-not-read')},'mock_must_not_read_real_credentials'),
    ({'mode':'live','mock_unknown':True},'live_must_not_use_mock_faults'),
])
def test_guards_precede_config_and_key_reads(patch,code):
    a=argparse.Namespace(mode='sdk-mock',execute_live=False,max_cny=None,credentials=None,mock_unknown=False,
        api_config=Path('must-not-read'),worker_config=Path('must-not-read'),owner_config=Path('must-not-read'))
    for k,v in patch.items():setattr(a,k,v)
    with pytest.raises(ValueError,match=code):entry()['preflight'](a)


def test_policy_fits_single_new_budget_and_does_not_require_confirmed_facts():
    m=entry();p=m['MaterialPolicy'].load(m['ROOT']/'configs/core0614-materials.json')
    assert m['PHASE']=='core-06-14-20261103-cny1' and m['LIMIT']==1_000_000_000
    assert p.max_http_attempts==p.max_operation_attempts==6
    assert p.reservation_nano*6<=m['LIMIT'] and p.model=='deepseek-flash'
    assert '项目还不完整' in m['SOURCE']


def test_stage15_uses_distinct_phase_and_policy_without_changing_stage14():
    m=entry();phase,profile=m['STAGES']['core-06-15']
    assert phase=='core-06-15-20261103-cny1' and phase!=m['PHASE']
    p=m['MaterialPolicy'].load(m['ROOT']/f'configs/{profile}-materials.json')
    assert p.max_operation_attempts==p.max_http_attempts==12
    assert p.reservation_nano*6<=m['LIMIT']
