"""Real new-project PG integration; no database substitute and no old TS configuration."""

import asyncio
import json
import os
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import AsyncConnection
from psycopg.rows import dict_row

from interview_agent.api.app import create_app
from interview_agent.contracts.practice import CreateTarget
from interview_agent.storage.config import DatabaseSettings, Settings
from interview_agent.storage.errors import ApiError
from interview_agent.storage.migrate import migrate

pytestmark = pytest.mark.postgres
PASSWORD = "Synthetic-password-42!"


def arun(coroutine):
    return asyncio.run(coroutine, loop_factory=asyncio.SelectorEventLoop)


@pytest.fixture(scope="session")
def pg_settings(project_root):
    path = os.environ.get("PY_INTERVIEW_TEST_CONFIG")
    if not path:
        pytest.skip("Set PY_INTERVIEW_TEST_CONFIG to the new private test.json for real PG integration")
    settings = Settings.load(path)
    assert settings.mode == "test" and settings.database.dbname == "py_interview_test"
    owner = DatabaseSettings.model_validate_json(Path(path).with_name("test-migration.json").read_text(encoding="utf-8"))
    async def prepare():
        runs = await asyncio.gather(migrate(owner, project_root / "backend/migrations"), migrate(owner, project_root / "backend/migrations"))
        # Current runs must not overwrite the historical PY-MIG-04 acceptance files.
        evidence = project_root / ".private/pg-current"
        evidence.mkdir(parents=True, exist_ok=True)
        (evidence / "migration-init.json").write_text(json.dumps({"concurrent_runs": runs}), encoding="utf-8")
    arun(prepare())
    return settings, owner


def client_for(settings):
    return TestClient(create_app(settings), base_url=settings.origin, raise_server_exceptions=False,
                      backend_options={"loop_factory": asyncio.SelectorEventLoop})


@pytest.fixture
def client(pg_settings):
    with client_for(pg_settings[0]) as value:
        value.headers["Origin"] = pg_settings[0].origin
        yield value


def signup(client, email=None):
    email = email or f"py-{uuid4().hex}@example.com"
    response = client.post("/api/auth/sign-up/email", json={"name": "Synthetic Student", "email": email, "password": PASSWORD})
    assert response.status_code == 200
    return response.json()


def create(client, key=None, focus=""):
    return client.post("/api/training-targets/", json={"role_package_id": "agent-intern", "level": "intern", "focus": focus},
                       headers={"Idempotency-Key": key or f"key-{uuid4().hex}"})


def query(owner, statement, params=()):
    async def execute():
        async with await AsyncConnection.connect(owner.conninfo(), autocommit=True, row_factory=dict_row) as conn:
            return await (await conn.execute(statement, params)).fetchall()
    return arun(execute())


def test_pg_registration_cookie_and_hash_storage(client, pg_settings):
    value = signup(client)
    token = client.cookies.get("py_interview_session")
    assert token and len(token) == 43
    session = client.get("/api/auth/get-session")
    assert session.status_code == 200 and session.json()["user"] == value["user"]
    row = query(pg_settings[1], "SELECT u.password_hash,s.token_hash FROM auth.users u JOIN auth.sessions s ON s.user_id=u.id WHERE s.id=%s", (value["session"]["id"],))[0]
    assert row["password_hash"].startswith("$argon2id$") and row["password_hash"] != PASSWORD
    assert row["token_hash"] != token and len(row["token_hash"]) == 64
    assert "password_hash" not in session.text and "token_hash" not in session.text and token not in session.text
    assert session.headers["cache-control"] == "no-store"


def test_pg_cookie_flags_and_new_token_on_login(client):
    email = f"py-{uuid4().hex}@example.com"
    response = client.post("/api/auth/sign-up/email", json={"name": "Test", "email": email, "password": PASSWORD})
    header = response.headers["set-cookie"].lower()
    assert "httponly" in header and "samesite=lax" in header and "max-age=28800" in header and "domain=" not in header
    old = client.cookies.get("py_interview_session")
    result = client.post("/api/auth/sign-in/email", json={"email": email.upper(), "password": PASSWORD})
    assert result.status_code == 200 and client.cookies.get("py_interview_session") != old


def test_pg_email_normalization_and_duplicate_registration(client):
    value = signup(client)
    duplicate = client.post("/api/auth/sign-up/email", json={"name": "Other", "email": value["user"]["email"].upper(), "password": PASSWORD})
    assert duplicate.status_code == 422 and duplicate.json()["code"] == "email_already_registered"


def test_pg_unknown_and_wrong_password_share_generic_error(client):
    value = signup(client)
    wrong = client.post("/api/auth/sign-in/email", json={"email": value["user"]["email"], "password": "wrong-synthetic-password"})
    unknown = client.post("/api/auth/sign-in/email", json={"email": f"missing-{uuid4().hex}@example.com", "password": PASSWORD})
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json() == {"code": "invalid_credentials"}


@pytest.mark.parametrize("patch", [{"email": "not-an-email"}, {"password": "short"}, {"name": "   "}, {"user_id": "forged"}])
def test_pg_auth_validation_never_echoes_password(client, patch):
    body = {"name": "Test", "email": f"test-{uuid4().hex}@example.com", "password": PASSWORD, **patch}
    result = client.post("/api/auth/sign-up/email", json=body)
    assert result.status_code == 400 and result.json() == {"code": "invalid_request"}
    assert PASSWORD not in result.text


@pytest.mark.parametrize("origin", ["https://untrusted.example", "null", ""])
def test_pg_origin_exact_match(client, origin):
    result = client.post("/api/auth/sign-out", json={}, headers={"Origin": origin})
    assert result.status_code == 403


def test_pg_body_limit_and_host_boundary(client):
    assert client.post("/api/auth/sign-up/email", content=b"x" * 17000, headers={"Content-Type": "application/json"}).status_code == 413
    assert client.get("/health", headers={"Host": "untrusted.example"}).status_code == 400
    assert client.get("/api/training-targets/").status_code == 401


def test_pg_logout_revokes_even_replayed_cookie_and_stale_principal(client):
    signup(client)
    token = client.cookies.get("py_interview_session")
    principal = client.portal.call(client.app.state.auth.resolve, token)[0]
    assert client.post("/api/auth/sign-out", json={}).status_code == 200
    assert client.get("/api/auth/get-session", headers={"Cookie": f"py_interview_session={token}"}).json() is None
    assert client.get("/api/training-targets/", headers={"Cookie": f"py_interview_session={token}"}).status_code == 401
    async def stale_write():
        with pytest.raises(ApiError) as error:
            await client.app.state.targets.create(principal, f"stale-{uuid4().hex}", CreateTarget(role_package_id="agent-intern", level="intern"))
        assert error.value.status == 401
    client.portal.call(stale_write)


def test_pg_expiry_is_server_authoritative(client, pg_settings):
    value = signup(client)
    query(pg_settings[1], "UPDATE auth.sessions SET created_at=clock_timestamp()-interval '2 hours',expires_at=clock_timestamp()-interval '1 hour' WHERE id=%s RETURNING id", (value["session"]["id"],))
    assert client.get("/api/auth/get-session").json() is None
    assert client.get("/api/training-targets/").status_code == 401


def test_pg_old_ts_cookie_and_random_cookie_do_not_authenticate(client):
    assert client.get("/api/auth/get-session", headers={"Cookie": "better-auth.session_token=synthetic-old-cookie"}).json() is None
    assert client.get("/api/auth/get-session", headers={"Cookie": "py_interview_session=" + "x" * 43}).json() is None


def test_pg_owner_scoped_resource_crud_and_soft_delete(client, pg_settings):
    user = signup(client)
    key = f"create-{uuid4().hex}"
    first = create(client, key, "initial")
    assert first.status_code == 201 and first.json()["user_id"] == user["user"]["id"]
    target = first.json(); resource = f"/api/training-targets/{target['id']}"
    assert client.get(resource).status_code == 200
    with client_for(pg_settings[0]) as other:
        other.headers["Origin"] = pg_settings[0].origin
        signup(other)
        assert other.get(resource).status_code == 404
        assert other.patch(resource, json={"expected_version": 1, "focus": "foreign"}).status_code == 404
        assert other.request("DELETE", resource, json={"expected_version": 1}).status_code == 404
    updated = client.patch(resource, json={"expected_version": 1, "focus": "updated"})
    assert updated.status_code == 200 and updated.json()["version"] == 2
    assert client.patch(resource, json={"expected_version": 1, "focus": "stale"}).status_code == 409
    assert client.request("DELETE", resource, json={"expected_version": 2}).status_code == 200
    assert client.get(resource).status_code == 404
    assert target["id"] not in [t["id"] for t in client.get("/api/training-targets/").json()["items"]]
    assert create(client, key, "initial").status_code == 404


def test_pg_idempotency_same_key_same_body_and_conflict(client):
    signup(client)
    key = f"idem-{uuid4().hex}"
    first, replay = create(client, key, "one"), create(client, key, "one")
    assert first.status_code == 201 and replay.status_code == 200 and first.json()["id"] == replay.json()["id"]
    assert create(client, key, "different").status_code == 409


@pytest.mark.parametrize("conflict", [False, True])
def test_pg_concurrent_idempotency_has_one_physical_row(client, pg_settings, conflict):
    signup(client)
    token = client.cookies.get("py_interview_session")
    key = f"parallel-{uuid4().hex}"
    async def scenario():
        principal = (await client.app.state.auth.resolve(token))[0]
        values = [CreateTarget(role_package_id="agent-intern", level="intern", focus="one"),
                  CreateTarget(role_package_id="agent-intern", level="intern", focus="different" if conflict else "one")]
        result = await asyncio.gather(*(client.app.state.targets.create(principal, key, value) for value in values), return_exceptions=True)
        if conflict:
            assert len([r for r in result if isinstance(r, ApiError) and r.status == 409]) == 1
        else:
            assert result[0][0].id == result[1][0].id and sorted(r[1] for r in result) == [False, True]
    client.portal.call(scenario)
    assert query(pg_settings[1], "SELECT count(*) AS count FROM app.training_targets WHERE request_id=%s", (key,))[0]["count"] == 1


def test_pg_concurrent_versions_have_one_winner(client):
    signup(client)
    target = create(client).json()
    token = client.cookies.get("py_interview_session")
    async def scenario():
        principal = (await client.app.state.auth.resolve(token))[0]
        result = await asyncio.gather(*(client.app.state.targets.change(principal, target["id"], 1, focus=text) for text in ["a", "b"]), return_exceptions=True)
        assert len([r for r in result if isinstance(r, ApiError) and r.status == 409]) == 1
        assert len([r for r in result if not isinstance(r, Exception) and r.version == 2]) == 1
    client.portal.call(scenario)


def test_pg_public_identity_and_parameter_binding(client):
    signup(client)
    body = {"role_package_id": "agent-intern", "level": "intern", "user_id": str(uuid4())}
    assert client.post("/api/training-targets/", json=body, headers={"Idempotency-Key": "valid-key-123"}).status_code == 400
    focus = "x'); DROP TABLE auth.users; --"
    result = create(client, focus=focus)
    assert result.status_code == 201 and result.json()["focus"] == focus
    assert client.get("/api/auth/get-session").json()["user"]


def test_pg_app_role_is_not_superuser_and_cannot_ddl(pg_settings):
    async def scenario():
        async with await AsyncConnection.connect(pg_settings[0].database.conninfo(), autocommit=True) as conn:
            row = await (await conn.execute("SELECT rolsuper,rolcreatedb,rolcreaterole FROM pg_roles WHERE rolname=current_user")).fetchone()
            assert row == (False, False, False)
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                await conn.execute("CREATE TABLE app.not_allowed(id integer)")
    arun(scenario())


def test_pg_migration_replay_hash_and_transactional_rollback(pg_settings, project_root, tmp_path):
    owner = pg_settings[1]
    directory = project_root / "backend/migrations"
    assert arun(migrate(owner, directory)) == []
    for path in directory.glob("*.sql"):
        (tmp_path / path.name).write_bytes(path.read_bytes())
    first = tmp_path / "001_auth.sql"
    first.write_bytes(first.read_bytes() + b"\n-- changed\n")
    with pytest.raises(ValueError, match="migration_checksum_mismatch"):
        arun(migrate(owner, tmp_path))
    first.write_bytes((directory / first.name).read_bytes())
    next_number = max(int(path.name[:3]) for path in directory.glob("[0-9][0-9][0-9]_*.sql")) + 1
    (tmp_path / f"{next_number:03d}_rollback_probe.sql").write_text("CREATE TABLE app.rollback_probe(id integer); SELECT 1/0;", encoding="utf-8")
    with pytest.raises(psycopg.errors.DivisionByZero):
        arun(migrate(owner, tmp_path))
    assert query(owner, "SELECT to_regclass('app.rollback_probe') AS table_name")[0]["table_name"] is None
    assert query(owner, "SELECT count(*) AS count FROM infra.schema_migrations")[0]["count"] == len(list(directory.glob("[0-9][0-9][0-9]_*.sql")))


def test_pg_registration_session_failure_rolls_back_user(client, pg_settings, monkeypatch):
    email = f"rollback-{uuid4().hex}@example.com"
    async def fail(*args):
        raise RuntimeError("SYNTHETIC-SESSION-FAILURE")
    monkeypatch.setattr(client.app.state.auth, "_new_session", fail)
    result = client.post("/api/auth/sign-up/email", json={"name": "Test", "email": email, "password": PASSWORD})
    assert result.status_code == 500 and result.json() == {"code": "internal_error"}
    assert query(pg_settings[1], "SELECT count(*) AS count FROM auth.users WHERE email=%s", (email,))[0]["count"] == 0


def test_pg_new_app_instance_reads_existing_sql_session(client, pg_settings):
    value = signup(client)
    token = client.cookies.get("py_interview_session")
    with client_for(pg_settings[0]) as another:
        response = another.get("/api/auth/get-session", headers={"Cookie": f"py_interview_session={token}"})
        assert response.status_code == 200 and response.json()["user"] == value["user"]
