"""Bounded flat Java workspace; inherits the JA-06 isolation and cleanup protocol."""
from .docker_java import DockerJava
from interview_agent.career.repository import digest

JAVA_FILES = frozenset({'Main.java', 'ExpiringCache.java', 'OrderService.java', 'OrderState.java'})


def validate_sources(files):
    if not isinstance(files, dict) or 'Main.java' not in files or not set(files) <= JAVA_FILES:
        raise ValueError('invalid_workspace_sources')
    if any(not isinstance(v, str) or not v.strip() or '\x00' in v or len(v.encode()) > 8192 for v in files.values()):
        raise ValueError('invalid_workspace_source')
    if sum(len(v.encode()) for v in files.values()) > 16384:
        raise ValueError('workspace_source_limit')


class _Compiler(DockerJava):
    def __init__(self, profile, files, **kwargs):
        super().__init__(profile, **kwargs)
        self.files = dict(files)

    async def stage_sources(self, compiler, source, lost):
        for name, text in sorted(self.files.items()):
            # Names come from JAVA_FILES, content is stdin data, never command text.
            await self.must(['exec', '-i', compiler, '/bin/sh', '-c', 'cat > /work/' + name], data=text.encode(), lost=lost)
        return sorted(self.files)


class JavaWorkspaceExecutor:
    def __init__(self, profile, *, docker=None):
        self.profile, self.docker = profile, docker

    async def grade(self, files, cases, lost=None):
        validate_sources(files)
        snapshot = dict(files)
        result = await _Compiler(self.profile, snapshot, docker=self.docker).grade(snapshot['Main.java'], cases, lost)
        result['evidence'].update(workspace_sha256=digest(snapshot), workspace_policy='ja07-flat-java-v1')
        return result
