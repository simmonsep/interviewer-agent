import io
import tarfile
import asyncio
import sys
import threading
import time
from types import SimpleNamespace
import pytest
from interview_agent.execution.docker_java import DockerJava,DockerProfile,clean_classes,ExecutionUnavailable
from interview_agent.written.catalog import task,public_task,catalog
from interview_agent.written.contracts import SubmitCode,CreateWritten
from interview_agent.written.repository import WrittenRepository

IMAGE='eclipse-temurin@sha256:'+'a'*64


def archive(entries):
    out=io.BytesIO()
    with tarfile.open(fileobj=out,mode='w') as tar:
        for name,kind in entries:
            entry=tarfile.TarInfo(name); entry.type=kind
            if kind==tarfile.REGTYPE: entry.size=8; tar.addfile(entry,io.BytesIO(b'\xca\xfe\xba\xbe1234'))
            else: entry.linkname='Main.class'; tar.addfile(entry)
    return out.getvalue()


@pytest.mark.parametrize('name,kind',[('../Main.class',tarfile.REGTYPE),('/Main.class',tarfile.REGTYPE),
    ('x/Main.class',tarfile.REGTYPE),('Main.class',tarfile.SYMTYPE),('Main.class',tarfile.LNKTYPE),('Main.java',tarfile.REGTYPE)])
def test_host_never_extracts_hostile_compiler_paths(name,kind):
    with pytest.raises(ExecutionUnavailable): clean_classes(archive([(name,kind)]))


def test_class_archive_is_rebuilt_with_only_bounded_flat_class_files():
    result=clean_classes(archive([('./Main.class',tarfile.REGTYPE),('Main$Inner.class',tarfile.REGTYPE)]))
    with tarfile.open(fileobj=io.BytesIO(result)) as t:
        assert t.getnames()==['Main.class','Main$Inner.class']
        assert all(x.mode==0o600 for x in t)
    with pytest.raises(ExecutionUnavailable): clean_classes(archive([('A.class',tarfile.REGTYPE)]))
    with pytest.raises(ExecutionUnavailable): clean_classes(archive([('Main.class',tarfile.REGTYPE)]*2))


def test_fixed_runtime_policy_does_not_accept_user_commands_or_mounts():
    executor=DockerJava(DockerProfile(image=IMAGE,policy='ja06-docker-java-v1'),docker='docker')
    args=executor.create_args('ja06-'+'b'*32)
    assert '--read-only' in args and '--network=none' in args and '--pull=never' in args
    assert '--cap-drop=ALL' in args and '--security-opt=no-new-privileges=true' in args
    assert '--memory=384m' in args and '--memory-swap=384m' in args and '--pids-limit=64' in args
    assert not any(x in args for x in ('--privileged','-v','--volume','--mount','--env-file'))
    assert args[-2:]==[IMAGE,'120']


def test_tasks_have_stable_frozen_versions_and_only_samples_are_public():
    for item in catalog():
        assert item['version']=='ja06-java-backend-v1' and len(item['samples'])==1
        assert 'analysis' not in item and 'cases' not in item
    a=task('idempotency-record'); a['cases'][0]['output']='spoof'
    assert task('idempotency-record')['cases'][0]['output']!='spoof'


@pytest.mark.parametrize('source',['','  ','\x00','中'*3000])
def test_source_is_byte_bounded_before_execution(source):
    with pytest.raises(ValueError): SubmitCode(expected_version=1,source=source)


def test_modes_and_arbitrary_runtime_options_cannot_be_supplied_by_candidate():
    with pytest.raises(ValueError): CreateWritten(expected_version=1,mode='coaching')
    with pytest.raises(ValueError): SubmitCode(expected_version=1,source='class Main {}',command='curl')


def test_grade_gate_requires_every_frozen_case_and_source_image_binding():
    t=task('idempotency-record'); row={'source_sha256':'source','input_payload':{'task':t,'policy':{'image':IMAGE,'execution_policy':'ja06-docker-java-v1'}}}
    result={'verdict':'ACCEPTED','cases':[{'id':c['id'],'visibility':c['visibility'],'verdict':'ACCEPTED'} for c in t['cases']],
            'evidence':{'source_sha256':'source','image':IMAGE,'policy':'ja06-docker-java-v1'}}
    repo=WrittenRepository(None); assert repo.validate_result(row,result)==result
    with pytest.raises(ValueError): repo.validate_result(row,{**result,'cases':result['cases'][:1]})
    with pytest.raises(ValueError): repo.validate_result(row,{**result,'evidence':{**result['evidence'],'image':'other'}})
    with pytest.raises(ValueError): repo.validate_result(row,{**result,'evidence':{**result['evidence'],'source_sha256':'changed'}})


def test_active_exam_filters_hidden_verdicts_and_analysis():
    value={'verdict':'WRONG_ANSWER','cases':[{'id':'sample','visibility':'public','verdict':'ACCEPTED'},
        {'id':'secret','visibility':'hidden','verdict':'WRONG_ANSWER'}],'evidence':{'image':IMAGE}}
    repo=WrittenRepository(None); visible=repo._visible_result(value,False)
    assert visible['execution_error'] is None and len(visible['public_cases'])==1 and 'WRONG_ANSWER' not in str(visible)
    assert repo._visible_result(value,True)==value


def test_controller_bounds_output_and_timeout_with_real_host_child_pipes():
    # Trusted test helper, never candidate code: exercises Docker CLI subprocess plumbing.
    executor=DockerJava(DockerProfile(image=IMAGE,policy='ja06-docker-java-v1'),docker=sys.executable)
    out=executor._run(['-c','import sys; sys.stdout.write("x"*1000000)'],limit=1024)
    assert out.reason=='OUTPUT_LIMIT' and len(out.stdout)+len(out.stderr)<=1024
    wait=executor._run(['-c','import time; time.sleep(10)'],seconds=.1)
    assert wait.reason=='TIME_LIMIT'


def test_controller_joins_host_process_on_async_cancellation():
    executor=DockerJava(DockerProfile(image=IMAGE,policy='ja06-docker-java-v1'),docker=sys.executable)
    async def run():
        t=asyncio.create_task(executor.command(['-c','import time; time.sleep(10)']))
        await asyncio.sleep(.1); start=time.monotonic(); t.cancel()
        with pytest.raises(asyncio.CancelledError): await t
        assert time.monotonic()-start<3
    asyncio.run(run())
