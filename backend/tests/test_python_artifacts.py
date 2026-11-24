"""Distribution contract tests: reject changed resources and private archive paths."""
import io
import json
from pathlib import Path
import runpy
import tarfile
import zipfile
import pytest


@pytest.mark.parametrize('fault',[None,'private-wheel','private-sdist','changed-migration','missing-notice'])
def test_artifact_contract_uses_original_bytes_and_rejects_private_paths(tmp_path,fault):
    files={
        'backend/migrations/001_test.sql':b'SELECT 1;', 'fixtures/pm0-05/example.json':b'{}',
        'fixtures/question-banks/example.json':b'{}','LICENSE':b'original project notice',
        'docs/third-party/NOTICE.md':b'notice','docs/third-party/Pi-LICENSE.txt':b'upstream notice',
        'docs/third-party/python-notices/licenses/example/LICENSE':b'dependency notice',
        'uv.lock':b'locked','.python-version':b'3.13.17','scripts/bootstrap-python.ps1':b'bootstrap',
        'manifests/PY-MIG-01-sources.json':b'{}','configs/python-postgres.json':b'{}','configs/python-redis.json':b'{}',
        'scripts/check-core-journey.py':b'journey',
        'scripts/check-python-artifacts.py':b'check', 'scripts/check-wheel-initialization.py':b'fresh'}
    root=tmp_path/'source'
    for name,data in files.items():
        p=root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(data)
    wheel=tmp_path/'test.whl';sdist=tmp_path/'test.tar.gz';metadata='test-0.1.dist-info'
    with zipfile.ZipFile(wheel,'w') as z:
        z.writestr(metadata+'/METADATA',b'License-Expression: MIT\n')
        for name,data in files.items():
            if name.startswith(('backend/','fixtures/')):
                z.writestr('interview_agent/_resources/'+name,b'changed' if fault=='changed-migration' and name.endswith('.sql') else data)
            elif name=='LICENSE' or name.startswith('docs/'):
                if not (fault=='missing-notice' and name.endswith('example/LICENSE')):z.writestr(metadata+'/licenses/'+name,data)
        if fault=='private-wheel':z.writestr('interview_agent/.private/.env.example',b'do not publish')
    with tarfile.open(sdist,'w:gz') as t:
        for name,data in files.items():
            info=tarfile.TarInfo('bundle/'+name);info.size=len(data);t.addfile(info,io.BytesIO(data))
        if fault=='private-sdist':
            info=tarfile.TarInfo('bundle/.private/old-copy/.env.example');info.size=0;t.addfile(info,io.BytesIO())
    check=runpy.run_path(str(Path(__file__).resolve().parents[2]/'scripts/check-python-artifacts.py'))['check']
    if fault:
        with pytest.raises((ValueError,KeyError)):check(wheel,sdist,root)
    else:
        result=check(wheel,sdist,root)
        assert result['resources_verified']==3 and not result['license_compatibility_or_release_approved']
