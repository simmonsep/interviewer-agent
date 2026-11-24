from interview_agent.resources import resource_root
from interview_agent.data.catalog import load_catalog


def test_repository_and_frozen_resource_paths(monkeypatch, project_root, tmp_path):
    assert resource_root() == project_root
    assert load_catalog(resource_root())
    monkeypatch.setattr('sys._MEIPASS', str(tmp_path), raising=False)
    assert resource_root() == tmp_path


def test_probe_rejects_development_before_database(monkeypatch):
    from types import SimpleNamespace
    import pytest
    from interview_agent.desktop.sidecar import pump
    from interview_agent.evaluation.adapters import arun
    monkeypatch.setattr('interview_agent.worker.config.WorkerSettings.load', lambda _: SimpleNamespace(mode='development'))
    with pytest.raises(ValueError, match='requires_test_environment'):
        arun(pump('unused', 'unused'))


def test_invalid_dependency_config_is_redacted_and_does_not_connect(tmp_path, monkeypatch):
    from interview_agent.desktop.sidecar import check_dependencies
    from interview_agent.evaluation.adapters import arun
    path = tmp_path / 'bad.json'
    path.write_text('{"password":"synthetic-secret",', encoding='utf-8')
    async def unexpected_connection(*args, **kwargs):
        raise AssertionError('database connection attempted for invalid config')
    monkeypatch.setattr('psycopg.AsyncConnection.connect', unexpected_connection)
    assert arun(check_dependencies(path, path)) == {'ready': False, 'code': 'configuration_invalid'}
