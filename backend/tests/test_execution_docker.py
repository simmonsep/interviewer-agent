"""Real local Docker only; source fixtures are synthetic and no model is called."""
import asyncio
import json
import os
import runpy
from pathlib import Path
import pytest
from interview_agent.execution.docker_java import DockerJava,DockerProfile
from interview_agent.written.catalog import task

pytestmark=pytest.mark.docker


@pytest.fixture(scope='module')
def profile(project_root):
    value=os.environ.get('PY_INTERVIEW_EXECUTOR_PROFILE')
    if not value: pytest.skip('Set PY_INTERVIEW_EXECUTOR_PROFILE after preparing the pinned local Docker image')
    p=DockerProfile.load(value)
    # No candidate compilation before the real isolation checks succeed.
    check=runpy.run_path(str(project_root/'scripts/check-executor.py'))['check']
    result=asyncio.run(check(p))
    evidence=project_root/'.private/ja-06/docker-isolation-current.json'
    evidence.parent.mkdir(parents=True,exist_ok=True);evidence.write_text(json.dumps(result,indent=2),encoding='utf-8')
    return p


def grade(profile,source,identity='idempotency-record'):
    return asyncio.run(DockerJava(profile).grade(source,task(identity)['cases']))


@pytest.mark.parametrize('identity,file',[('idempotency-record','IdempotencyMain.java'),('lock-owner-expiry','LockMain.java')])
def test_actual_java_compiles_and_passes_host_owned_assertions(profile,project_root,identity,file):
    result=grade(profile,(project_root/'fixtures/ja06'/file).read_text(encoding='utf-8'),identity)
    assert result['verdict']=='ACCEPTED' and len(result['cases'])==4
    assert result['evidence']['image']==profile.image


@pytest.mark.parametrize('source,expected',[
    ('public class Main { public static void main(String[] a) { System.out.println("PASS"); } }','WRONG_ANSWER'),
    ('public class Main { syntax error }','COMPILE_ERROR'),
    ('public class Main { public static void main(String[] a) { throw new RuntimeException("synthetic"); } }','RUNTIME_ERROR'),
    ('public class Main { public static void main(String[] a) { while(true) {} } }','TIME_LIMIT'),
    ('public class Main { public static void main(String[] a) { while(true) System.out.println("x".repeat(1024)); } }','OUTPUT_LIMIT'),
    ('public class Main { public static void main(String[] a) throws Exception { new ProcessBuilder("/bin/bash","-c","s=x; while true; do s=\\\"$s$s\\\"; done").inheritIO().start().waitFor(); } }','MEMORY_LIMIT'),
])
def test_actual_outcomes_are_distinct_and_printing_pass_cannot_spoof_judge(profile,source,expected):
    result=grade(profile,source);assert result['verdict']==expected,result


def test_actual_cancel_removes_container_and_returns_no_acceptance(profile):
    async def run():
        stop=asyncio.Event();executor=DockerJava(profile)
        # Observe the actual Java runtime, not just the earlier compiler container.
        async def cancel():
            for _ in range(100):
                ids=await executor.must(['ps','-q','--filter','label=interview-agent.executor=ja06'])
                for identity in ids.decode().split():
                    top=await executor.command(['top',identity,'-eo','pid,args'])
                    if b'java -Xms' in top.stdout:stop.set();return
                await asyncio.sleep(.1)
            raise AssertionError('Java runtime never appeared')
        timer=asyncio.create_task(cancel())
        result=await executor.grade('public class Main { public static void main(String[] a) { while(true) {} } }',task('idempotency-record')['cases'],stop)
        await timer;assert result['verdict']=='CANCELLED'
        assert not (await executor.must(['ps','-aq','--filter','label=interview-agent.executor=ja06'])).strip()
    asyncio.run(run())
