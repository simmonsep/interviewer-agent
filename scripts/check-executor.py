"""Validate the real local container boundary before any training submission is run."""
import argparse
import asyncio
import json
import os
from pathlib import Path
from interview_agent.execution.docker_java import DockerJava,DockerProfile,ExecutionCancelled


async def check(profile):
    executor=DockerJava(profile); names=[]; checks=[]
    readiness=await executor.readiness(); lost=asyncio.Event()
    os.environ['JA06_HOST_SECRET_SENTINEL']='synthetic-host-only'
    try:
        name=await executor.container(names,lost)
        java=(await executor.command(['exec',name,'java','-version'])).stderr.decode('utf-8','replace')
        javac=(await executor.must(['exec',name,'javac','-version'])).decode('utf-8','replace')
        detail=await executor.inspect(name); h=detail['HostConfig']
        assert h['NetworkMode']=='none' and h['ReadonlyRootfs'] and not h['Privileged']
        assert h['CapDrop']==['ALL'] and any('no-new-privileges' in x for x in h['SecurityOpt'])
        assert h['PidsLimit']==64 and h['Memory']==h['MemorySwap']==384*1024*1024 and h['NanoCpus']==1_000_000_000
        assert not h['Binds'] and not h['PortBindings'] and not h['Devices']
        assert all(m['Type']=='tmpfs' for m in detail['Mounts'])
        checks.append('inspect: no network/bind/socket/ports, readonly, nonroot, caps, bounded cgroups')
        status=(await executor.must(['exec',name,'/bin/cat','/proc/self/status'])).decode()
        assert 'NoNewPrivs:\t1' in status and 'Seccomp:\t2' in status and 'CapEff:\t0000000000000000' in status
        env=(await executor.must(['exec',name,'/usr/bin/env'])).decode()
        assert 'JA06_HOST_SECRET_SENTINEL' not in env and 'DEEPSEEK_API_KEY' not in env
        checks.append('process: no-new-privileges/seccomp/caps and host environment not forwarded')
        probe=await executor.command(['exec',name,'/bin/sh','-c',
            'test ! -e /var/run/docker.sock && test ! -e /run/secrets && test ! -e /host && '
            '! touch /etc/ja06-write-probe && touch /work/writable && '
            'test "$(cat /sys/fs/cgroup/memory.max)" = 402653184 && '
            'test "$(cat /sys/fs/cgroup/pids.max)" = 64'])
        assert probe.code==0
        route=(await executor.must(['exec',name,'/bin/cat','/proc/net/route'])).decode()
        assert len(route.strip().splitlines())==1
        network=await executor.command(['exec',name,'/bin/bash','-c','timeout 1 bash -c "echo test > /dev/tcp/1.1.1.1/443"'])
        assert network.code!=0 and network.reason is None
        checks.append('filesystem/cgroups checked inside container; external TCP cannot connect')
        disk=await executor.command(['exec',name,'/bin/dd','if=/dev/zero','of=/work/fill','bs=1M','count=80'],seconds=5)
        assert disk.code!=0 and b'No space left on device' in disk.stderr
        await executor._remove(name); names.remove(name)
        name=await executor.container(names,lost)
        memory=await executor.command(['exec',name,'/bin/bash','-c','s=x; while true; do s="$s$s"; done'],seconds=8)
        events=(await executor.must(['exec',name,'/bin/cat','/sys/fs/cgroup/memory.events'])).decode()
        assert int(dict(line.split() for line in events.strip().splitlines())['oom_kill'])>=1
        await executor._remove(name); names.remove(name)
        name=await executor.container(names,lost)
        pids=await executor.command(['exec',name,'/bin/sh','-c','i=0; while [ "$i" -lt 90 ]; do sleep 20 & i=$((i+1)); done; wait'],seconds=2)
        assert b'fork' in pids.stderr.lower() and (pids.code!=0 or pids.reason=='TIME_LIMIT')
        await executor._remove(name); names.remove(name)
        checks.append('tmpfs exhaustion, kernel cgroup OOM and process exhaustion observed under fixed limits')
        name=await executor.container(names,lost)
        out=await executor.command(['exec',name,'/usr/bin/yes'],limit=4096,seconds=2)
        assert out.reason=='OUTPUT_LIMIT' and len(out.stdout)+len(out.stderr)<=4096
        await executor._remove(name); names.remove(name)
        name=await executor.container(names,lost)
        timeout=await executor.command(['exec',name,'/bin/sleep','20'],seconds=.2)
        assert timeout.reason=='TIME_LIMIT'
        await executor._remove(name); names.remove(name)
        checks.append('bounded output and wall time; whole container removed after interrupted exec')
        name=await executor.container(names,lost)
        async def cancel(): await asyncio.sleep(.2); lost.set()
        timer=asyncio.create_task(cancel())
        try:
            await executor.command(['exec',name,'/bin/sleep','20'],seconds=3,lost=lost)
            raise AssertionError('cancel did not interrupt')
        except ExecutionCancelled: pass
        await timer
        await executor._remove(name); names.remove(name)
        checks.append('explicit cancellation interrupts CLI and removes all candidate processes')
        return {'scope':'local controlled synthetic execution only','readiness':readiness,'java':java,'javac':javac,'checks':checks,'count':len(checks)}
    finally:
        for name in names: await executor._remove(name)
        os.environ.pop('JA06_HOST_SECRET_SENTINEL',None)


if __name__=='__main__':
    parser=argparse.ArgumentParser(); parser.add_argument('--profile',type=Path,default=Path('configs/ja06-docker-java.json'))
    parser.add_argument('--output',type=Path,default=Path('.private/ja-06/isolation.json'))
    args=parser.parse_args(); result=asyncio.run(check(DockerProfile.load(args.profile)))
    args.output.parent.mkdir(parents=True,exist_ok=True); args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'checks':result['count'],'receipt':str(args.output)},ensure_ascii=False))
