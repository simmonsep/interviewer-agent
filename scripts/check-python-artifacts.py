"""Check core wheel resources and source archive boundaries; never approve a release."""
import argparse
import hashlib
import json
from pathlib import Path,PurePosixPath
import tarfile
import zipfile

ROOT=Path(__file__).resolve().parents[1]
BLOCKED={'.private','.venv','.bootstrap','.uv-cache','.git','node_modules','__pycache__'}


def check(wheel,sdist,root=ROOT):
    def safe(names):
        for name in names:
            p=PurePosixPath(name)
            if p.is_absolute() or '..' in p.parts or BLOCKED.intersection(p.parts):raise ValueError('private_runtime_or_unsafe_path_in_artifact')
            if p.name=='.env' or (p.name.startswith('.env.') and p.name!='.env.example'):raise ValueError('environment_file_in_artifact')
    resources=[*sorted((root/'backend/migrations').glob('*.sql')),
               *sorted((root/'fixtures/pm0-05').rglob('*')),*sorted((root/'fixtures/question-banks').rglob('*'))]
    resources=[p for p in resources if p.is_file()]
    notices=[root/'LICENSE',root/'docs/third-party/NOTICE.md',root/'docs/third-party/Pi-LICENSE.txt',
             *sorted((root/'docs/third-party/python-notices').rglob('*'))]
    notices=[p for p in notices if p.is_file()]
    with zipfile.ZipFile(wheel) as z:
        safe(z.namelist())
        metadata=next(n.rsplit('/',1)[0] for n in z.namelist() if n.endswith('.dist-info/METADATA'))
        if b'License-Expression: MIT' not in z.read(metadata+'/METADATA'):raise ValueError('project_license_metadata_missing')
        for p in resources:
            if z.read('interview_agent/_resources/'+p.relative_to(root).as_posix())!=p.read_bytes():raise ValueError('wheel_resource_bytes_changed')
        for p in notices:
            if z.read(metadata+'/licenses/'+p.relative_to(root).as_posix())!=p.read_bytes():raise ValueError('wheel_notice_bytes_changed')
    with tarfile.open(sdist) as t:
        safe(t.getnames());prefix=t.getnames()[0].split('/')[0]
        bootstrap=['uv.lock','.python-version','scripts/bootstrap-python.ps1','manifests/PY-MIG-01-sources.json',
                   'configs/python-postgres.json','configs/python-redis.json','scripts/check-core-journey.py',
                   'scripts/check-python-artifacts.py','scripts/check-wheel-initialization.py']
        for p in [*resources,*notices,*[root/n for n in bootstrap]]:
            f=t.extractfile(prefix+'/'+p.relative_to(root).as_posix())
            if f is None or f.read()!=p.read_bytes():raise ValueError('source_distribution_bytes_changed')
    return {'status':'passed','wheel_sha256':hashlib.sha256(wheel.read_bytes()).hexdigest(),
            'sdist_sha256':hashlib.sha256(sdist.read_bytes()).hexdigest(),'resources_verified':len(resources),
            'migrations_verified':len(list((root/'backend/migrations').glob('*.sql'))),'notice_files_verified':len(notices),
            'private_runtime_paths_excluded':True,'license_compatibility_or_release_approved':False}


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--wheel',type=Path,required=True)
    p.add_argument('--sdist',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    args=p.parse_args();report=check(args.wheel,args.sdist)
    args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_bytes((json.dumps(report,indent=2)+'\n').encode())
    print(json.dumps(report))
