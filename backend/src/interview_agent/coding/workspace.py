"""In-memory snapshots, explicit paths and compare-and-swap edits; no host paths."""
import difflib
import hashlib
from copy import deepcopy
from interview_agent.career.repository import digest
from interview_agent.execution.java_workspace import validate_sources


def file_hash(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


class Workspace:
    def __init__(self, task):
        self.task = deepcopy(task)
        self.initial = deepcopy(task['files']); self.files = deepcopy(self.initial)
        self.revision = 1; self.edits = []

    @property
    def sha256(self):
        return digest(self.files)

    def sources(self):
        return {k: v for k, v in self.files.items() if k.endswith('.java')}

    def read(self, path):
        if not isinstance(path, str) or path not in self.files:
            raise ValueError('file_not_allowed')
        return {'path': path, 'text': self.files[path], 'sha256': file_hash(self.files[path]), 'editable': path in self.task['editable']}

    def replace(self, path, expected_sha256, text, *, actor):
        if path not in self.task['editable'] or path not in self.files:
            raise ValueError('file_not_editable')
        if actor not in ('user', 'assistant'):
            raise ValueError('invalid_actor')
        before = self.files[path]
        if file_hash(before) != expected_sha256:
            raise ValueError('file_version_conflict')
        if not isinstance(text, str) or not text.strip() or '\x00' in text or len(text.encode()) > 8192:
            raise ValueError('invalid_file_content')
        candidate = {**self.sources(), path: text}; validate_sources(candidate)
        if before == text:
            return {'changed': False, 'workspace_sha256': self.sha256, 'revision': self.revision}
        if len(self.edits) >= 3:
            raise ValueError('coding_edit_limit')
        delta = ''.join(difflib.unified_diff(before.splitlines(True), text.splitlines(True), fromfile='a/' + path, tofile='b/' + path))
        self.files[path] = text; self.revision += 1
        self.edits.append({'revision': self.revision, 'actor': actor, 'path': path,
                           'before_sha256': file_hash(before), 'after_sha256': file_hash(text), 'diff': delta})
        return {'changed': True, 'workspace_sha256': self.sha256, 'revision': self.revision}

    def snapshot(self):
        return {'task_id': self.task['id'], 'task_sha256': self.task['sha256'], 'revision': self.revision,
                'workspace_sha256': self.sha256, 'files': deepcopy(self.files), 'edits': deepcopy(self.edits)}
