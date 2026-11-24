"""The frozen sidecar's `worker --kind` routing, offline.

S01 added `interview-prep`; without this table entry the shipped desktop would never run the
preparation Worker, so the one-click pipeline would stop at "plan pending" forever.
"""
import importlib

import pytest

from interview_agent.desktop import sidecar


@pytest.mark.parametrize('kind,module', [
    ('interview', 'interview_agent.interviews.worker'),
    ('interview-prep', 'interview_agent.interview_prep.worker'),
    ('materials', 'interview_agent.materials.worker'),
    ('career', 'interview_agent.career.worker'),
])
def test_each_worker_kind_starts_its_own_module(kind, module, monkeypatch):
    target = importlib.import_module(module)
    seen = {}

    def fake_run(args):
        seen['args'] = args

        async def nothing():
            return None

        return nothing()

    monkeypatch.setattr(target, 'run', fake_run)
    monkeypatch.setattr(sidecar.asyncio, 'run', lambda coro, **kwargs: coro.close())
    sidecar.worker(kind, 'worker.json', 'scripted')
    assert seen['args'].config == 'worker.json' and seen['args'].provider == 'scripted'
    assert seen['args'].once is False


def test_an_unknown_worker_kind_is_refused(monkeypatch):
    import sys

    monkeypatch.setattr(sys, 'argv', ['interview-sidecar', 'worker', '--config', 'x.json', '--kind', 'nope'])
    with pytest.raises(SystemExit):
        sidecar.main()
