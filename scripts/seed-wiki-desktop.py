"""Create one synthetic pending Wiki version through the real workers with mock HTTP."""
import argparse
import json
import sys
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'backend/src'), str(ROOT / 'backend/tests')]

from interview_agent.interview_prep import checkpointer
from interview_agent.storage.config import DatabaseSettings, Settings
from interview_agent.storage.migrate import migrate
from interview_agent.worker.config import WorkerSettings
from test_interview_wiki import goal_for, items, start, with_source
from test_postgres import PASSWORD, arun, client_for, signup


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    output = parser.parse_args().output.resolve()
    if not output.is_relative_to(ROOT / '.private'):
        parser.error('use a fresh output directory under .private')
    private = ROOT / '.private'
    api = Settings.load(private / 'postgres/test.json')
    worker = WorkerSettings.load(private / 'redis/test-worker.json')
    owner = DatabaseSettings.model_validate_json(
        (private / 'postgres/test-migration.json').read_text(encoding='utf-8'))
    assert api.mode == worker.mode == 'test'
    assert api.database == worker.database and api.database.dbname == owner.dbname == 'py_interview_test'
    output.mkdir(parents=True)
    arun(migrate(owner, ROOT / 'backend/migrations'))
    arun(checkpointer.initialize(owner, api.database.user))
    with client_for(api) as client:
        client.headers['Origin'] = api.origin
        user = signup(client, f'f170-wiki-{uuid4().hex}@desktop.example.com')['user']
        goal = goal_for(client)
        plan = with_source(client, owner, worker, goal, output)
        original = start(client, worker, goal, plan)
        [version] = items(client)
        assert version['status'] == 'pending'
        (output / 'seed.json').write_text(json.dumps({
            'user_id': user['id'], 'email': user['email'], 'password': PASSWORD,
            'first_session_id': original['id'], 'version_id': version['id'],
            'question': version['document']['question'],
        }, ensure_ascii=False), encoding='utf-8')
    print('synthetic_pending_wiki_ready')


if __name__ == '__main__':
    main()
