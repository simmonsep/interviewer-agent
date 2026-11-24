"""Single new post-answer task; fixed legacy entry stays available."""
import argparse
from pathlib import Path
import pytest
from test_live_interview_entry import entry


def test_new_stage_has_own_identity_policy_and_original_four_request_cap():
    m=entry();phase,profile=m['STAGES']['core-06-11']
    assert phase=='core-06-11-20261103-cny05' and profile=='core0611'
    p=m['InterviewPolicy'].load(m['ROOT']/f'configs/{profile}-interviews.json')
    assert p.max_http_attempts==p.max_operation_attempts==4 and m['LIMIT']==500_000_000
    assert m['STAGES']['core-06-06'][0]==m['PHASE']


@pytest.mark.parametrize('patch,code',[
    ({'stage':'arbitrary'},'unsupported_stage'),
    ({'stage':'core-06-11','mode':'live','execute_live':True,'max_cny':'1.5','credentials':Path('unused')},'live_requires_explicit_stage_approval_flags'),
    ({'stage':'core-06-11','mock_fault':'reject-once'},'unsupported_mock_fault_for_stage'),
])
def test_new_stage_rejections_precede_configuration_or_key_reads(patch,code):
    args=argparse.Namespace(stage='core-06-11',mode='sdk-mock',execute_live=False,max_cny=None,credentials=None,mock_fault='none')
    for k,v in patch.items():setattr(args,k,v)
    with pytest.raises(ValueError,match=code):entry()['preflight'](args)
