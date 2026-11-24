"""S04A A1: pin the real HTTP shapes of auth, operation status and model settings.

The assertions run first against the existing routes, then the response models added in this
batch must keep every key, type and absent-field rule identical. Synthetic account, test PG.
"""
import json
from uuid import uuid4

import pytest

from interview_agent.model_settings.connectivity import ProbeError
from interview_agent.model_settings import routes as settings_routes
from psycopg import AsyncConnection

from test_postgres import PASSWORD, arun, client_for, pg_settings  # noqa: F401 (fixtures)


def cleanup(owner, user_id):
    statements = (
        "DELETE FROM app.business_events WHERE user_id=%s",
        "DELETE FROM app.assessment_revisions WHERE user_id=%s",
        "DELETE FROM app.assessment_candidates c USING app.operations o "
        "WHERE c.operation_id=o.id AND o.user_id=%s",
        "DELETE FROM app.outbox_events e USING app.background_jobs j,app.operations o "
        "WHERE e.job_id=j.id AND j.operation_id=o.id AND o.user_id=%s",
        "DELETE FROM app.background_jobs j USING app.operations o "
        "WHERE j.operation_id=o.id AND o.user_id=%s",
        "DELETE FROM app.operations WHERE user_id=%s",
        "DELETE FROM app.answer_attempts WHERE user_id=%s",
        "DELETE FROM app.practice_sessions WHERE user_id=%s",
        "DELETE FROM app.training_targets WHERE user_id=%s",
        "DELETE FROM auth.users WHERE id=%s")

    async def run():
        async with await AsyncConnection.connect(owner.conninfo(), autocommit=True) as conn:
            for statement in statements:
                await conn.execute(statement, (user_id,))

    arun(run())


@pytest.fixture
def client(pg_settings):
    users = []
    with client_for(pg_settings[0]) as value:
        value.headers["Origin"] = pg_settings[0].origin
        yield value, users
    for user in users:
        cleanup(pg_settings[1], user)


def sign_up(client, users):
    response = client.post("/api/auth/sign-up/email", json={
        "name": "Contract Student", "email": f"py-contract-{uuid4().hex}@example.com",
        "password": PASSWORD})
    assert response.status_code == 200, response.text
    users.append(response.json()["user"]["id"])
    return response


def test_anonymous_session_is_json_null_and_sign_up_sets_the_cookie(client):
    client, users = client
    anonymous = client.get("/api/auth/get-session")
    assert anonymous.status_code == 200
    assert anonymous.json() is None

    created = sign_up(client, users)
    body = created.json()
    assert set(body) == {"user", "session"}
    assert set(body["user"]) == {"id", "email", "name"}
    assert set(body["session"]) == {"id", "userId", "expiresAt"}
    assert "password" not in json.dumps(body).lower()
    cookie = created.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie

    current = client.get("/api/auth/get-session")
    assert current.json() == body
    client.post("/api/auth/sign-out")
    assert client.get("/api/auth/get-session").json() is None


def test_sign_in_and_sign_out_keep_the_same_body(pg_settings, client):
    client, users = client
    created = sign_up(client, users).json()
    logout = client.post("/api/auth/sign-out")
    assert logout.status_code == 200 and logout.json() == {"success": True}
    assert "set-cookie" in logout.headers

    again = client.post("/api/auth/sign-in/email", json={
        "email": created["user"]["email"], "password": PASSWORD})
    assert again.status_code == 200
    assert set(again.json()) == {"user", "session"}
    assert again.json()["user"]["id"] == created["user"]["id"]

    wrong = client.post("/api/auth/sign-in/email", json={
        "email": created["user"]["email"], "password": "wrong-password-123"})
    assert wrong.status_code == 401 and wrong.json() == {"code": "invalid_credentials"}


def test_operation_replay_keeps_one_public_shape(client):
    client, users = client
    sign_up(client, users)
    target = client.post("/api/training-targets/", json={
        "role_package_id": "agent-intern", "level": "intern", "focus": "Java"},
        headers={"Idempotency-Key": "a1-target-" + uuid4().hex}).json()
    body = {"expected_version": target["version"], "focus": "Java 并发"}
    key = "a1-focus-" + uuid4().hex
    first = client.post(f"/api/training-targets/{target['id']}/focus-operations",
                        json=body, headers={"Idempotency-Key": key})
    replay = client.post(f"/api/training-targets/{target['id']}/focus-operations",
                         json=body, headers={"Idempotency-Key": key})
    assert (first.status_code, replay.status_code) == (202, 200)
    assert first.json() == replay.json()
    view = first.json()
    assert set(view) == {"id", "user_id", "resource_id", "kind", "expected_version", "status",
                         "result", "error_code", "created_at", "completed_at"}
    assert view["kind"] == "update_target_focus" and view["status"] == "PENDING"
    assert view["result"] is None and view["error_code"] is None
    assert view["completed_at"] is None
    # The input text is not echoed in the task projection.
    assert "Java 并发" not in json.dumps(view, ensure_ascii=False)


def goal(client, direction="Java后端"):
    response = client.post("/api/career-goals/", headers={"Idempotency-Key": uuid4().hex},
                           json={"message": "准备 Java 后端面试，无简历也可以。",
                                 "conditions": {"direction": direction}})
    assert response.status_code in (200, 201, 202), response.text
    return response.json()


def test_interview_202_and_replay_declare_the_same_view(client):
    client, users = client
    sign_up(client, users)
    g = goal(client)
    body = {"expected_version": 1, "role": "java-backend", "max_questions": 2}
    key = "a2-interview-" + uuid4().hex
    first = client.post(f"/api/career-goals/{g['id']}/interviews", json=body,
                        headers={"Idempotency-Key": key})
    replay = client.post(f"/api/career-goals/{g['id']}/interviews", json=body,
                         headers={"Idempotency-Key": key})
    assert (first.status_code, replay.status_code) == (202, 200)
    assert first.json() == replay.json()
    view = first.json()
    assert set(view) == {
        "id", "goal_version", "material_revision", "version", "revision", "status", "policy",
        "pack_version", "target", "application", "material_fact_ids", "draft_id", "deadline_at",
        "material_alignment", "question_banks", "skills", "strategies", "events", "observations", "report", "run"}
    assert view["status"] == "PENDING" and view["policy"]["role"] == "java-backend"
    assert view["events"] == [] and view["report"] is None
    assert view["run"]["status"] == "PENDING" and view["run"]["origin"] is None

    listed = client.get(f"/api/career-goals/{g['id']}/interviews")
    assert listed.status_code == 200
    assert set(listed.json()) == {"items"} and listed.json()["items"][0]["id"] == view["id"]

    stopped = client.post(f"/api/career-goals/{g['id']}/interviews/{view['id']}/stop",
                          json={"expected_version": view["version"]})
    assert stopped.status_code == 200
    assert stopped.json()["status"] == "COMPLETED"
    assert stopped.json()["version"] == view["version"] + 1


def test_preparation_create_and_read_have_one_shape(client):
    client, users = client
    sign_up(client, users)
    g = goal(client)
    body = {"expected_version": 1, "role": "java-backend", "max_questions": 2}
    key = "a2-prep-" + uuid4().hex
    first = client.post(f"/api/career-goals/{g['id']}/interview-preparations", json=body,
                        headers={"Idempotency-Key": key})
    replay = client.post(f"/api/career-goals/{g['id']}/interview-preparations", json=body,
                         headers={"Idempotency-Key": key})
    assert (first.status_code, replay.status_code) == (202, 200)
    assert first.json() == replay.json()
    view = first.json()
    assert set(view) == {
        "id", "status", "goal_version", "version", "role", "mode", "max_questions",
        "duration_seconds", "draft_id", "plan", "plan_sha256", "decision", "session_id",
        # F07Z-166: the explicit "no resume" flag round-trips; F07Z-167: the frozen profile.
        "use_resume", "profile",
        # S05 L3-4: the read-only window into what this preparation froze for cross-session use.
        "frozen_memory", "error_code", "attempt", "created_at"}
    assert view["status"] in {"PENDING", "RUNNING"} and view["session_id"] is None
    read = client.get(f"/api/career-goals/{g['id']}/interview-preparations/{view['id']}")
    assert read.status_code == 200 and read.json()["id"] == view["id"]


def test_material_upload_and_rewrite_share_the_listing_shape(client):
    client, users = client
    sign_up(client, users)
    g = goal(client)
    base = f"/api/career-goals/{g['id']}/materials"
    uploaded = client.post(base + "/sources", headers={"Idempotency-Key": uuid4().hex},
                           json={"expected_version": 1, "expected_revision": 0,
                                 "name": "resume.md", "format": "markdown",
                                 "text": "# 张三\n\n## 工作经历\n- 负责 SQL 事务与 Redis 通知。"})
    assert uploaded.status_code == 201, uploaded.text
    body = uploaded.json()
    assert {"source_id", "goal_version", "material_revision", "target", "model_requests",
            "material_model_requests", "sources", "facts", "runs", "drafts"} == set(body)
    assert set(body["sources"][0]) == {
        "id", "name", "format", "text", "material_revision", "content_sha256",
        "clarification_context", "file_reference"}

    rewritten = client.post(base + "/rewrite", headers={"Idempotency-Key": uuid4().hex},
                            json={"expected_version": 1, "expected_revision": 1})
    assert rewritten.status_code == 202, rewritten.text
    assert set(rewritten.json()) == (set(body) - {"source_id"}) | {"run_id"}
    run = rewritten.json()["runs"][0]
    assert run["mode"] == "draft" and run["status"] in {"PENDING", "RUNNING"}
    assert set(run) == {"id", "status", "goal_version", "material_revision", "revision",
                        "model_requests", "result", "error_code", "origin", "mode", "stale"}


def test_material_export_is_still_markdown_with_a_download_name(client):
    client, users = client
    sign_up(client, users)
    g = goal(client)
    base = f"/api/career-goals/{g['id']}/materials"
    # No draft exists yet: the route must keep its 404 and must not be swallowed by a model.
    missing = client.get(base + f"/drafts/{uuid4()}.md")
    assert missing.status_code == 404 and missing.json() == {"code": "resource_unavailable"}


def test_openapi_declares_models_media_types_and_both_success_statuses(pg_settings, tmp_path):
    from interview_agent.api.app import create_app

    plain = create_app(pg_settings[0]).openapi()["paths"]
    desktop = create_app(pg_settings[0], model_config_path=tmp_path / "model.json").openapi()["paths"]
    assert "/api/runtime/model-settings" not in plain
    assert "/api/runtime/model-settings" in desktop

    created = plain["/api/career-goals/{goal_id}/interviews"]["post"]["responses"]
    first = created["200"]["content"]["application/json"]["schema"]
    replay = created["202"]["content"]["application/json"]["schema"]
    assert first == replay  # one success model for first creation and replay
    assert first["$ref"].endswith("/InterviewView")

    events = plain["/api/operations/{operation_id}/events"]["get"]["responses"]["200"]
    assert "text/event-stream" in events["content"]
    markdown = plain["/api/career-goals/{goal_id}/materials/drafts/{draft_id}.md"]["get"]["responses"]["200"]
    assert "text/markdown" in markdown["content"]
    assert plain["/api/auth/sign-up/email"]["post"]["responses"]["200"]["content"][
        "application/json"]["schema"]["$ref"].endswith("/SessionView")
    assert "401" in plain["/api/training-targets/"]["get"]["responses"]


def test_shared_dependencies_keep_auth_and_idempotency_errors(client):
    client, users = client
    anonymous = client.get("/api/training-targets/")
    assert anonymous.status_code == 401 and anonymous.json() == {"code": "authentication_required"}

    sign_up(client, users)
    missing = client.post("/api/training-targets/", json={
        "role_package_id": "agent-intern", "level": "intern"})
    assert missing.status_code == 400 and missing.json() == {"code": "idempotency_key_required"}

    target = client.post("/api/training-targets/", headers={"Idempotency-Key": "a3-target-" + uuid4().hex},
                         json={"role_package_id": "agent-intern", "level": "intern"}).json()
    key = "a3-conflict-" + uuid4().hex
    first = client.post(f"/api/training-targets/{target['id']}/focus-operations",
                        json={"expected_version": target["version"], "focus": "Java"},
                        headers={"Idempotency-Key": key})
    assert first.status_code == 202
    conflict = client.post(f"/api/training-targets/{target['id']}/focus-operations",
                           json={"expected_version": target["version"], "focus": "SQL"},
                           headers={"Idempotency-Key": key})
    assert conflict.status_code == 409 and conflict.json() == {"code": "idempotency_conflict"}


def test_model_settings_never_echo_the_key(pg_settings, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from interview_agent.api.app import create_app

    monkeypatch.setenv("INTERVIEW_KEY_VAULT", "off")
    model = tmp_path / "model.json"
    with TestClient(create_app(pg_settings[0], model_config_path=model),
                    base_url=pg_settings[0].origin, raise_server_exceptions=False,
                    backend_options={"loop_factory": __import__("asyncio").SelectorEventLoop}) as client:
        client.headers["Origin"] = pg_settings[0].origin
        users = []
        user = sign_up(client, users).json()["user"]["id"]
        try:
            empty = client.get("/api/runtime/model-settings")
            assert empty.status_code == 200
            assert empty.json()["enabled"] is False
            assert empty.json()["key_masked"] == ""

            saved = client.put("/api/runtime/model-settings", json={
                "provider": "deepseek", "base_url": "https://api.deepseek.com",
                "model": "deepseek-chat", "api_key": "sk-contract-123456789",
                "enabled": True, "live_cap_cny": 0.5})
            assert saved.status_code == 200, saved.text
            assert saved.json()["saved"] is True
            assert "sk-contract-123456789" not in saved.text
            assert saved.json()["key_masked"] == "sk-…6789"
            assert saved.json()["live_cap_cny"] == 0.5

            shown = client.get("/api/runtime/model-settings")
            assert shown.status_code == 200
            text = shown.text
            assert "sk-contract-123456789" not in text
            assert shown.json()["key_masked"] == "sk-…6789"
            assert set(shown.json()) == {
                "provider", "base_url", "model", "key_masked", "key_storage", "presets",
                "enabled", "live_authorization", "live_cap_cny", "live_state",
                "live_spent_cny", "live_remaining_cny", "material_enabled",
                "material_live_authorization", "material_live_cap_cny", "material_state",
                "material_spent_cny", "material_remaining_cny", "prep_enabled",
                "prep_live_authorization", "prep_live_cap_cny", "prep_state",
                "prep_spent_cny", "prep_remaining_cny", "active", "material_active",
                "assessment_mode", "vault_available"}

            removed = client.delete("/api/runtime/model-settings")
            assert removed.status_code == 200
            assert removed.json() == {"deleted": True, "vault_cleared": False, "enabled": False,
                                      "material_enabled": False, "key_masked": "", "active": False,
                                      "material_active": False}
            assert "sk-contract" not in model.read_text(encoding="utf-8")
        finally:
            cleanup(pg_settings[1], user)


def test_probe_returns_success_fields_or_a_stable_error(pg_settings, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from interview_agent.api.app import create_app

    monkeypatch.setenv("INTERVIEW_KEY_VAULT", "off")
    model = tmp_path / "model.json"
    calls = []

    async def fake_probe(**kwargs):
        calls.append(kwargs)
        return {"ok": True, "latency_ms": 12, "model": kwargs["model"],
                "base_url": kwargs["base_url"], "reply_preview": "可用"}

    monkeypatch.setattr(settings_routes, "probe", fake_probe)
    with TestClient(create_app(pg_settings[0], model_config_path=model),
                    base_url=pg_settings[0].origin, raise_server_exceptions=False,
                    backend_options={"loop_factory": __import__("asyncio").SelectorEventLoop}) as client:
        client.headers["Origin"] = pg_settings[0].origin
        users = []
        user = sign_up(client, users).json()["user"]["id"]
        try:
            ok = client.post("/api/runtime/model-settings/test", json={
                "base_url": "https://api.deepseek.com", "model": "deepseek-chat",
                "api_key": "sk-offline-contract"})
            assert ok.status_code == 200
            assert ok.json() == {"ok": True, "latency_ms": 12, "model": "deepseek-chat",
                                 "base_url": "https://api.deepseek.com", "reply_preview": "可用"}
            assert calls and calls[0]["key"] == "sk-offline-contract"

            async def failed(**_kwargs):
                raise ProbeError("model_key_rejected")

            monkeypatch.setattr(settings_routes, "probe", failed)
            bad = client.post("/api/runtime/model-settings/test", json={
                "base_url": "https://api.deepseek.com", "model": "deepseek-chat",
                "api_key": "sk-offline-contract"})
            assert bad.status_code == 200
            assert bad.json() == {"ok": False, "code": "model_key_rejected",
                                  "base_url": "https://api.deepseek.com", "model": "deepseek-chat"}
        finally:
            cleanup(pg_settings[1], user)
