"""The acceptance entry must reject unsafe profiles before migrations or HTTP."""
import argparse
import importlib.util
import json
from pathlib import Path
import pytest
from test_postgres import arun


def entry():
    path=Path(__file__).resolve().parents[2]/'scripts/check-core-journey.py'
    spec=importlib.util.spec_from_file_location('core_journey_entry',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('change',['development','main_owner','database_mismatch'])
def test_rejects_non_test_or_mismatched_profiles_before_side_effects(tmp_path,change):
    db={'host':'127.0.0.1','port':55432,'dbname':'py_interview_test','user':'py_interview_app','password':'synthetic-only'}
    api={'database':db,'mode':'test'}
    worker={**api,'redis':{'host':'127.0.0.1','port':55380,'db':1,'password':'synthetic-only'}}
    owner={**db,'user':'py_interview_owner'}
    if change=='development':api={**api,'mode':'development'}
    elif change=='main_owner':owner={**owner,'dbname':'py_interview'}
    else:api={**api,'database':{**db,'port':55433}}
    paths=[]
    for name,value in [('api',api),('worker',worker),('owner',owner)]:
        p=tmp_path/(name+'.json');p.write_bytes(json.dumps(value).encode());paths.append(p)
    args=argparse.Namespace(api_config=paths[0],worker_config=paths[1],migration_config=paths[2],output=tmp_path/'outputs')
    with pytest.raises(ValueError,match='explicit_test_environment_required|database_profiles_must_match'):
        arun(entry().run(args))
    assert not args.output.exists()
