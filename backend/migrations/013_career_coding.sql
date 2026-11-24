ALTER TABLE app.career_goals ADD COLUMN coding_runs integer NOT NULL DEFAULT 0 CHECK(coding_runs BETWEEN 0 AND 24);
ALTER TABLE app.career_goals ADD COLUMN coding_executions integer NOT NULL DEFAULT 0 CHECK(coding_executions BETWEEN 0 AND 36);
ALTER TABLE app.career_goals ADD COLUMN coding_assistant_responses integer NOT NULL DEFAULT 0 CHECK(coding_assistant_responses BETWEEN 0 AND 48);
CREATE TABLE app.career_coding_sessions (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), goal_id uuid NOT NULL REFERENCES app.career_goals(id) ON DELETE CASCADE,
 goal_version integer NOT NULL, request_id text NOT NULL, request_hash text NOT NULL, input_payload jsonb NOT NULL,
 version integer NOT NULL DEFAULT 1, revision bigint NOT NULL DEFAULT 1, workspace_revision integer NOT NULL DEFAULT 1,
 status text NOT NULL DEFAULT 'ACTIVE' CHECK(status IN ('ACTIVE','SUBMITTED','COMPLETED','CANCELLED','STALE','EXPIRED')),
 runs integer NOT NULL DEFAULT 0 CHECK(runs BETWEEN 0 AND 8), assist_runs integer NOT NULL DEFAULT 0 CHECK(assist_runs BETWEEN 0 AND 2),
 executions integer NOT NULL DEFAULT 0 CHECK(executions BETWEEN 0 AND 12),
 assistant_responses integer NOT NULL DEFAULT 0 CHECK(assistant_responses BETWEEN 0 AND 24),
 deadline_at timestamptz NOT NULL, stop_reason text, created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 UNIQUE(goal_id,request_id)
);
CREATE TABLE app.career_coding_versions (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), session_id uuid NOT NULL REFERENCES app.career_coding_sessions(id) ON DELETE CASCADE,
 revision integer NOT NULL CHECK(revision BETWEEN 1 AND 12), origin text NOT NULL CHECK(origin IN ('starter','user','assistant')),
 files jsonb NOT NULL, files_sha256 text NOT NULL, edits jsonb NOT NULL, explanation text NOT NULL,
 source_run_id uuid, created_at timestamptz NOT NULL DEFAULT clock_timestamp(), UNIQUE(session_id,revision)
);
CREATE TABLE app.career_coding_runs (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), job_id uuid NOT NULL DEFAULT gen_random_uuid() UNIQUE,
 goal_id uuid NOT NULL REFERENCES app.career_goals(id) ON DELETE CASCADE, goal_version integer NOT NULL,
 session_id uuid NOT NULL REFERENCES app.career_coding_sessions(id) ON DELETE CASCADE,
 kind text NOT NULL CHECK(kind IN ('public','assist','final')), workspace_revision integer NOT NULL,
 request_id text NOT NULL, request_hash text NOT NULL, input_payload jsonb NOT NULL,
 status text NOT NULL DEFAULT 'PENDING' CHECK(status IN ('PENDING','RUNNING','SUCCEEDED','FAILED','CANCELLED')),
 attempt integer NOT NULL DEFAULT 0 CHECK(attempt BETWEEN 0 AND 3), fencing_token bigint NOT NULL DEFAULT 0,
 started boolean NOT NULL DEFAULT false, lease_until timestamptz, dispatched_at timestamptz, dispatch_count integer NOT NULL DEFAULT 0,
 executions integer NOT NULL DEFAULT 0 CHECK(executions BETWEEN 0 AND 3),
 assistant_responses integer NOT NULL DEFAULT 0 CHECK(assistant_responses BETWEEN 0 AND 12),
 candidate jsonb, result jsonb, error_code text, created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 UNIQUE(session_id,request_id), CHECK((status='RUNNING')=(lease_until IS NOT NULL)),
 FOREIGN KEY(session_id,workspace_revision) REFERENCES app.career_coding_versions(session_id,revision)
);
ALTER TABLE app.career_coding_versions ADD FOREIGN KEY(source_run_id) REFERENCES app.career_coding_runs(id);
CREATE TABLE app.career_coding_commands (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), session_id uuid NOT NULL REFERENCES app.career_coding_sessions(id) ON DELETE CASCADE,
 kind text NOT NULL CHECK(kind IN ('edit','accept','reject','cancel')),
 request_id text NOT NULL, request_hash text NOT NULL, input_payload jsonb NOT NULL,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(), UNIQUE(session_id,request_id)
);
CREATE INDEX career_coding_dispatch_idx ON app.career_coding_runs(status,lease_until,dispatched_at);
CREATE FUNCTION app.career_coding_input_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF TG_TABLE_NAME IN ('career_coding_versions','career_coding_commands') THEN
  IF NEW IS DISTINCT FROM OLD THEN RAISE EXCEPTION 'coding_append_only' USING ERRCODE='23514'; END IF;
 ELSE
  IF (NEW.goal_id,NEW.goal_version,NEW.request_id,NEW.request_hash,NEW.input_payload) IS DISTINCT FROM
     (OLD.goal_id,OLD.goal_version,OLD.request_id,OLD.request_hash,OLD.input_payload) THEN
   RAISE EXCEPTION 'coding_input_immutable' USING ERRCODE='23514';
  END IF;
  IF TG_TABLE_NAME='career_coding_runs' THEN
   IF (NEW.session_id,NEW.kind,NEW.workspace_revision,NEW.job_id) IS DISTINCT FROM
      (OLD.session_id,OLD.kind,OLD.workspace_revision,OLD.job_id) THEN
    RAISE EXCEPTION 'coding_run_binding_immutable' USING ERRCODE='23514';
   END IF;
  END IF;
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER coding_session_immutable BEFORE UPDATE ON app.career_coding_sessions FOR EACH ROW EXECUTE FUNCTION app.career_coding_input_immutable();
CREATE TRIGGER coding_version_immutable BEFORE UPDATE ON app.career_coding_versions FOR EACH ROW EXECUTE FUNCTION app.career_coding_input_immutable();
CREATE TRIGGER coding_run_immutable BEFORE UPDATE ON app.career_coding_runs FOR EACH ROW EXECUTE FUNCTION app.career_coding_input_immutable();
CREATE TRIGGER coding_command_immutable BEFORE UPDATE ON app.career_coding_commands FOR EACH ROW EXECUTE FUNCTION app.career_coding_input_immutable();
GRANT SELECT,INSERT,UPDATE,DELETE ON app.career_coding_sessions,app.career_coding_versions,app.career_coding_runs,app.career_coding_commands TO py_interview_app;
