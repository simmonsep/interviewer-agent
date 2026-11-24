"""F07Z-131 (F07Z-121 E1): every HTTP failure the framework raises keeps the `{"code": ...}` shape.

The page's `describeError` only reads `code`; Starlette's default `{"detail": "Not Found"}` left
a dead end with no message. These tests pin the mapping so a future FastAPI/Starlette bump cannot
quietly reintroduce the old shape.
"""
from fastapi.testclient import TestClient

from interview_agent.api.app import create_app


def test_an_unknown_route_returns_code_not_found():
    client = TestClient(create_app())
    response = client.get('/api/does-not-exist')
    assert response.status_code == 404
    assert response.json() == {'code': 'not_found'}
    assert response.headers['cache-control'] == 'no-store'


def test_a_wrong_method_returns_code_method_not_allowed():
    client = TestClient(create_app())
    response = client.put('/health')
    assert response.status_code == 405
    assert response.json() == {'code': 'method_not_allowed'}
    assert response.headers['cache-control'] == 'no-store'
