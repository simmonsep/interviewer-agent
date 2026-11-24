"""Stage limits and preflight must precede any credentials or live network access."""
import argparse
from pathlib import Path
import runpy
import pytest


def entry():return runpy.run_path(str(Path(__file__).resolve().parents[2]/'scripts/check-live-core.py'))


@pytest.mark.parametrize('patch,code',[
    ({'mode':'live'},'live_requires_explicit_stage_approval_flags'),
    ({'mode':'live','execute_live':True,'max_cny':'3','credentials':Path('unused')},'live_requires_explicit_stage_approval_flags'),
    ({'credentials':Path('must-not-read')},'mock_must_not_read_real_credentials'),
    ({'mode':'live','mock_unknown':True},'live_must_not_use_mock_faults'),
    ({'stage':'arbitrary-new-id'},'unsupported_stage'),
])
def test_preflight_refuses_unsafe_options_before_configuration_or_key_reads(patch,code):
    args=argparse.Namespace(stage='core-06-03',mode='sdk-mock',execute_live=False,max_cny=None,credentials=None,mock_unknown=False,
                            api_config=Path('must-not-read'),worker_config=Path('must-not-read'),owner_config=Path('must-not-read'))
    for k,v in patch.items():setattr(args,k,v)
    with pytest.raises(ValueError,match=code):entry()['preflight'](args)


@pytest.mark.parametrize('stage',['core-06-03','core-06-05'])
def test_role_allocations_and_request_caps_agree_with_frozen_plan(stage):
    module=entry();root=Path(__file__).resolve().parents[2]
    assert sum(module['LIMITS'].values())==2_000_000_000
    phase,profile=module['STAGES'][stage]
    assert phase==('core-06-03-20261031-cny2' if stage=='core-06-03' else 'core-06-05-20261101-cny2')
    policies=[cls.load(root/f'configs/{profile}-{role}.json') for role,cls in [
        ('career',module['CareerPolicy']),('materials',module['MaterialPolicy']),('interviews',module['InterviewPolicy'])]]
    assert [p.max_http_attempts for p in policies]==[6,4,12] and sum(p.max_http_attempts for p in policies)==22
    assert all(p.model=='deepseek-flash' and p.thinking=='disabled' for p in policies)
