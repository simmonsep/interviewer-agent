"""Fixed Docker protocol. Expected answers never enter candidate containers."""

import asyncio
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tarfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from pydantic import Field
from interview_agent.contracts.common import Contract

LABEL = 'interview-agent.executor=ja06'
POLICY = 'ja06-docker-java-v1'
JVM = ['-Xms16m', '-Xmx96m', '-Xss256k', '-XX:MaxMetaspaceSize=64m',
       '-XX:ReservedCodeCacheSize=32m', '-XX:CompressedClassSpaceSize=32m',
       '-XX:MaxDirectMemorySize=32m', '-XX:+UseSerialGC', '-XX:ActiveProcessorCount=1',
       '-XX:-UsePerfData', '-Djava.io.tmpdir=/tmp']


class ExecutionUnavailable(Exception):
    def __init__(self, code='executor_unavailable'): self.code = code; super().__init__(code)


class ExecutionCancelled(Exception): pass


class DockerProfile(Contract):
    image: str = Field(pattern=r'^eclipse-temurin@sha256:[a-f0-9]{64}$')
    policy: str = Field(pattern=r'^ja06-docker-java-v1$')

    @classmethod
    def load(cls, path): return cls.model_validate_json(Path(path).read_text(encoding='utf-8'))


@dataclass(frozen=True)
class ProcessResult:
    code: int
    stdout: bytes
    stderr: bytes
    reason: str | None = None


def clean_classes(raw):
    """Treat compiler output as hostile data, not paths to extract on the host."""
    if len(raw) > 2 * 1024 * 1024: raise ExecutionUnavailable('class_archive_limit')
    result = io.BytesIO(); names = set(); total = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(raw), mode='r:') as source, tarfile.open(fileobj=result, mode='w:') as target:
            for member in source:
                if member.isdir() and member.name in ('.', './'): continue
                name = member.name.removeprefix('./')
                if (not member.isfile() or not re.fullmatch(r'[A-Za-z_$][A-Za-z0-9_$]*\.class', name)
                    or name in names or len(names) >= 64 or not 0 < member.size <= 1024 * 1024):
                    raise ExecutionUnavailable('invalid_class_archive')
                total += member.size
                if total > 1024 * 1024: raise ExecutionUnavailable('class_archive_limit')
                data = source.extractfile(member).read()
                if not data.startswith(b'\xca\xfe\xba\xbe'): raise ExecutionUnavailable('invalid_class_archive')
                safe = tarfile.TarInfo(name); safe.size = len(data); safe.mode = 0o600
                target.addfile(safe, io.BytesIO(data)); names.add(name)
    except (tarfile.TarError, OSError, EOFError):
        raise ExecutionUnavailable('invalid_class_archive') from None
    if 'Main.class' not in names: raise ExecutionUnavailable('main_class_missing')
    return result.getvalue()


class DockerJava:
    """One compiler container and a fresh runtime per test, all without bind mounts."""
    def __init__(self, profile, *, docker=None):
        self.profile = profile
        self.docker = docker or shutil.which('docker')
        if not self.docker: raise ExecutionUnavailable('docker_missing')

    def _run(self, args, *, data=None, seconds=10, limit=16384, lost=None):
        if lost is not None and lost.is_set(): raise ExecutionCancelled()
        flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        try:
            p = subprocess.Popen([self.docker, *args], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, creationflags=flags)
        except OSError: raise ExecutionUnavailable('docker_start_failed') from None
        chunks = [bytearray(), bytearray()]; size = 0; lock = threading.Lock(); overflow = threading.Event()

        def read(pipe, index):
            nonlocal size
            try:
                while part := pipe.read1(4096):
                    with lock:
                        room = max(0, limit - size); chunks[index].extend(part[:room]); size += len(part)
                        if size > limit: overflow.set(); break
            except (OSError, ValueError): pass

        def write():
            try:
                if data: p.stdin.write(data); p.stdin.flush()
            except (OSError, ValueError): pass
            finally:
                try: p.stdin.close()
                except OSError: pass

        threads = [threading.Thread(target=read, args=(p.stdout, 0), daemon=True),
                   threading.Thread(target=read, args=(p.stderr, 1), daemon=True),
                   threading.Thread(target=write, daemon=True)]
        for t in threads: t.start()
        started = time.monotonic(); reason = None
        try:
            while p.poll() is None:
                if lost is not None and lost.is_set(): reason = 'CANCELLED'; break
                if overflow.is_set(): reason = 'OUTPUT_LIMIT'; break
                if time.monotonic() - started >= seconds: reason = 'TIME_LIMIT'; break
                time.sleep(.03)
            if reason: p.kill()
            try: p.wait(timeout=2)
            except subprocess.TimeoutExpired: p.kill(); p.wait(timeout=2)
            for t in threads: t.join(timeout=1)
            if overflow.is_set() and reason is None: reason = 'OUTPUT_LIMIT'
            if reason == 'CANCELLED': raise ExecutionCancelled()
            return ProcessResult(p.returncode, bytes(chunks[0]), bytes(chunks[1]), reason)
        finally:
            if p.poll() is None: p.kill()
            for pipe in (p.stdout, p.stderr): pipe.close()

    async def command(self, args, **kwargs):
        interrupted = threading.Event(); original = kwargs.pop('lost', None)
        class Stop:
            def is_set(self): return interrupted.is_set() or (original is not None and original.is_set())
        job = asyncio.create_task(asyncio.to_thread(self._run, args, lost=Stop(), **kwargs))
        try: return await asyncio.shield(job)
        except asyncio.CancelledError:
            interrupted.set()
            try: await asyncio.shield(job)
            except ExecutionCancelled: pass
            raise

    async def must(self, args, **kwargs):
        r = await self.command(args, **kwargs)
        if r.code or r.reason: raise ExecutionUnavailable('docker_command_failed')
        return r.stdout

    def create_args(self, name):
        return ['create', '--name', name, '--label', LABEL, '--label', f'interview-agent.expires={int(time.time()) + 150}',
                '--pull=never', '--runtime=runc', '--network=none', '--read-only', '--user=65532:65532',
                '--cap-drop=ALL', '--security-opt=no-new-privileges=true', '--pids-limit=64',
                '--memory=384m', '--memory-swap=384m', '--cpus=1',
                '--ulimit=nofile=128:128', '--ulimit=core=0:0',
                '--tmpfs=/work:rw,nosuid,nodev,noexec,size=64m,uid=65532,gid=65532,mode=700',
                '--tmpfs=/tmp:rw,nosuid,nodev,noexec,size=32m,uid=65532,gid=65532,mode=700',
                '--env=HTTP_PROXY=', '--env=HTTPS_PROXY=', '--env=NO_PROXY=', '--env=ALL_PROXY=',
                '--env=http_proxy=', '--env=https_proxy=', '--env=no_proxy=', '--env=all_proxy=',
                '--log-driver=none', '--workdir=/work', '--entrypoint=/bin/sleep',
                self.profile.image, '120']

    async def _remove(self, name):
        r = await self.command(['rm', '-f', name], seconds=10)
        # A failed create can mean there was never a container; independently verify absence.
        remaining = await self.must(['ps', '-aq', '--filter', f'name=^/{name}$'], seconds=5)
        if remaining.strip(): raise ExecutionUnavailable('executor_cleanup_failed')

    async def container(self, names, lost):
        name = 'ja06-' + uuid4().hex; names.append(name)
        await self.must(self.create_args(name), lost=lost)
        c = await self.inspect(name); h = c['HostConfig']
        if (h.get('Privileged') or not h.get('ReadonlyRootfs') or h.get('NetworkMode') != 'none'
            or h.get('Binds') or any(m.get('Type') != 'tmpfs' for m in c.get('Mounts', [])) or h.get('Devices') or h.get('PidMode')
            or c['Config'].get('User') != '65532:65532' or h.get('PidsLimit') != 64
            or h.get('Memory') != 384*1024*1024 or h.get('MemorySwap') != 384*1024*1024):
            raise ExecutionUnavailable('container_policy_mismatch')
        await self.must(['start', name], lost=lost)
        return name

    async def inspect(self, name):
        return json.loads(await self.must(['inspect', name], limit=65536))[0]

    async def readiness(self):
        info = json.loads(await self.must(['info', '--format', '{{json .}}'], limit=65536))
        if (info.get('OSType') != 'linux' or info.get('CgroupVersion') != '2'
            or not any('seccomp' in x and 'unconfined' not in x for x in info.get('SecurityOptions', []))):
            raise ExecutionUnavailable('linux_cgroup2_seccomp_required')
        image = json.loads(await self.must(['image', 'inspect', self.profile.image], limit=65536))[0]
        if image.get('Os') != 'linux' or image.get('Config', {}).get('Volumes'):
            raise ExecutionUnavailable('unsupported_executor_image')
        # Reap only our expired containers, never business containers or a still-live attempt.
        ids = (await self.must(['ps', '-aq', '--filter', f'label={LABEL}'])).decode().split()
        for identity in ids:
            if not re.fullmatch('[a-f0-9]{12,64}', identity): raise ExecutionUnavailable('invalid_container_id')
            c = await self.inspect(identity); expiry = c['Config']['Labels'].get('interview-agent.expires', '')
            if expiry.isdigit() and int(expiry) < time.time(): await self._remove(c['Name'].lstrip('/'))
        return {'policy': POLICY, 'image': self.profile.image, 'image_id': image['Id'],
                'docker_version': info['ServerVersion'], 'cgroup': info['CgroupVersion'], 'security': info['SecurityOptions']}

    async def stage_sources(self, compiler, source, lost):
        """Fixed single-file protocol; the workspace adapter supplies a bounded variant."""
        await self.must(['exec', '-i', compiler, '/bin/sh', '-c', 'cat > /work/Main.java'], data=source.encode(), lost=lost)
        return ['Main.java']

    async def grade(self, source, cases, lost=None):
        """cases are trusted host data: {id, input, output, visibility}."""
        if not isinstance(source, str) or not 0 < len(source.encode('utf-8')) <= 8192 or '\x00' in source:
            raise ValueError('invalid_source')
        names = []; outcomes = []; started = time.monotonic(); external_lost = lost
        class Budget:
            def is_set(self): return (external_lost is not None and external_lost.is_set()) or time.monotonic()-started >= 90
        lost = Budget()
        evidence = {'policy': POLICY, 'image': self.profile.image,
                    'source_sha256': hashlib.sha256(source.encode()).hexdigest()}
        async def execute():
            await self.readiness()
            compiler = await self.container(names, lost)
            sources = await self.stage_sources(compiler, source, lost)
            compiled = await self.command(['exec', compiler, 'javac', '-J-Xmx128m', '-J-XX:+UseSerialGC', '-J-XX:ActiveProcessorCount=1',
                                           '-proc:none', '--release', '21', '-encoding', 'UTF-8', '-d', '/work',
                                           *['/work/' + name for name in sources]], seconds=15, lost=lost)
            if compiled.reason or compiled.code:
                return {'verdict': compiled.reason or 'COMPILE_ERROR', 'diagnostic': compiled.stderr.decode('utf-8', 'replace')[:2000], 'cases': []}
            # Copy only the work directory containing source and class files; source is removed first.
            await self.must(['exec', compiler, '/bin/rm', *['/work/' + name for name in sources]], lost=lost)
            # docker cp's archive view cannot read this tmpfs mount. Export inside the
            # running container, then validate/rebuild the untrusted tar on the host.
            raw = await self.must(['exec', compiler, '/bin/tar', '-cf', '-', '-C', '/work', '.'], limit=2*1024*1024, lost=lost)
            try: archive = clean_classes(raw)
            except ExecutionUnavailable as error:
                if error.code in ('invalid_class_archive','main_class_missing','class_archive_limit'):
                    return {'verdict':'COMPILE_ERROR','diagnostic':'Use a default-package Main class; at most 64 flat class files / 1MiB compiled output.','cases':[]}
                raise
            await self._remove(compiler); names.remove(compiler)
            for case in cases:
                runtime = await self.container(names, lost)
                await self.must(['exec', '-i', runtime, '/bin/tar', '-xf', '-', '-C', '/work'], data=archive, lost=lost)
                r = await self.command(['exec', '-i', runtime, 'java', *JVM, '-cp', '/work', 'Main'], data=case['input'].encode(), seconds=3, lost=lost)
                state = (await self.inspect(runtime))['State']
                memory = await self.command(['exec', runtime, '/bin/cat', '/sys/fs/cgroup/memory.events'], lost=lost) if state.get('Running') else None
                # PID exhaustion may prevent a diagnostic exec; it must not mask the primary timeout/exit.
                memory_events = memory.stdout.decode() if memory is not None and memory.code==0 and not memory.reason else ''
                kernel_oom = dict(line.split() for line in memory_events.strip().splitlines()).get('oom_kill', '0')
                if state.get('OOMKilled') or int(kernel_oom)>0: verdict = 'MEMORY_LIMIT'
                elif r.reason: verdict = r.reason
                elif r.code: verdict = 'RUNTIME_ERROR'
                else:
                    actual = r.stdout.decode('utf-8', 'replace').replace('\r\n', '\n').rstrip('\n')
                    verdict = 'ACCEPTED' if actual == case['output'].rstrip('\n') else 'WRONG_ANSWER'
                outcomes.append({'id': case['id'], 'visibility': case['visibility'], 'verdict': verdict})
                await self._remove(runtime); names.remove(runtime)
                if verdict in ('TIME_LIMIT', 'OUTPUT_LIMIT', 'MEMORY_LIMIT', 'RUNTIME_ERROR'): break
            overall = next((x['verdict'] for x in outcomes if x['verdict'] != 'ACCEPTED'), 'ACCEPTED')
            return {'verdict': overall, 'cases': outcomes}
        try:
            try:
                result = await execute()
            except ExecutionCancelled:
                result = {'verdict': 'CANCELLED' if external_lost is not None and external_lost.is_set() else 'ENVIRONMENT_ERROR',
                          'code': 'cancelled' if external_lost is not None and external_lost.is_set() else 'executor_total_timeout', 'cases': outcomes}
            except ExecutionUnavailable as error: result = {'verdict': 'ENVIRONMENT_ERROR', 'code': error.code, 'cases': outcomes}
        finally:
            # Never report a successful grade while candidate containers remain.
            for name in list(names): await asyncio.shield(self._remove(name))
        return {**result, 'evidence': evidence}
