"""F07Z-129: the contract guards the F07Z-121 plan asked for, now that the migration is done.

`test_api_contract.py` (S04A) pins the real bodies of specific endpoints. This file guards the
*whole surface*, which is what a mechanical migration needs:

  1. every operation that returns JSON declares a response model (so FastAPI filters unknown
     fields instead of leaking a `SELECT *` row later);
  2. no module reaches for the legacy `Depends(...)` syntax again;
  3. authentication and the idempotency key each have exactly one definition;
  4. every operation documents at least one error status.
"""
import os
from pathlib import Path

import pytest

from interview_agent.api.app import create_app
from interview_agent.storage.config import Settings

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / 'backend' / 'src' / 'interview_agent'
HTTP = ('get', 'post', 'put', 'patch', 'delete')


def spec():
    path = os.environ.get('PY_INTERVIEW_TEST_CONFIG')
    if not path:
        pytest.skip('Set PY_INTERVIEW_TEST_CONFIG to the private test config')
    return create_app(Settings.load(path)).openapi()


def test_every_json_operation_declares_a_response_model():
    document = spec()
    missing = []
    for route, operations in document['paths'].items():
        for method, operation in operations.items():
            if method not in HTTP:
                continue
            responses = operation.get('responses') or {}
            success = [code for code in responses if code.startswith('2')]
            has_json = any(responses[code].get('content', {}).get('application/json')
                           for code in success)
            pure_empty = success == ['204'] or (success and not has_json and '204' in success)
            if not has_json and not pure_empty:
                missing.append('%s %s' % (method.upper(), route))
    assert not missing, 'operations without a response model: %s' % ', '.join(sorted(missing))


def test_no_module_uses_the_legacy_depends_syntax():
    offenders = []
    for path in SRC.rglob('*.py'):
        if path == SRC / 'api' / 'dependencies.py':
            continue
        if 'Depends(' in path.read_text(encoding='utf-8'):
            offenders.append(str(path.relative_to(ROOT)))
    assert not offenders, 'legacy Depends() left in: %s' % ', '.join(offenders)


def test_principal_and_idempotency_key_are_defined_once():
    principal = [path for path in SRC.rglob('*.py')
                 if 'def current_principal' in path.read_text(encoding='utf-8')]
    key = [path for path in SRC.rglob('*.py')
           if 'def idempotency_key' in path.read_text(encoding='utf-8')]
    assert principal == [SRC / 'api' / 'dependencies.py'], principal
    assert key == [SRC / 'api' / 'dependencies.py'], key


def test_every_operation_documents_an_error_status():
    # These two read or clear the session cookie and never raise: get-session returns null when
    # there is no session (that is the contract), sign-out always succeeds. Documenting a 401
    # they cannot emit would describe a different endpoint.
    exempt = {'GET /api/auth/get-session', 'POST /api/auth/sign-out'}
    document = spec()
    missing = []
    for route, operations in document['paths'].items():
        for method, operation in operations.items():
            if method not in HTTP or route == '/health':
                continue
            if '%s %s' % (method.upper(), route) in exempt:
                continue
            statuses = set(operation.get('responses') or {})
            if not any(code.startswith(('4', '5')) for code in statuses):
                missing.append('%s %s' % (method.upper(), route))
    assert not missing, 'operations without documented errors: %s' % ', '.join(sorted(missing))
