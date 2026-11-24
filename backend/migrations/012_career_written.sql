ALTER TABLE app.career_goals ADD COLUMN written_submissions integer NOT NULL DEFAULT 0 CHECK(written_submissions BETWEEN 0 AND 12);
CREATE TABLE app.career_written_sessions (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), goal_id uuid NOT NULL REFERENCES app.career_goals(id) ON DELETE CASCADE,
 goal_version integer NOT NULL, request_id text NOT NULL, request_hash text NOT NULL, input_payload jsonb NOT NULL,
 version integer NOT NULL DEFAULT 1, revision bigint NOT NULL DEFAULT 1,
 status text NOT NULL DEFAULT 'ACTIVE' CHECK(status IN ('ACTIVE','COMPLETED','CANCELLED','STALE')),
 submissions integer NOT NULL DEFAULT 0 CHECK(submissions BETWEEN 0 AND 3),
 deadline_at timestamptz NOT NULL, stop_reason text, created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 UNIQUE(goal_id,request_id)
);
CREATE TABLE app.career_written_submissions (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), job_id uuid NOT NULL DEFAULT gen_random_uuid() UNIQUE,
 session_id uuid NOT NULL REFERENCES app.career_written_sessions(id) ON DELETE CASCADE,
 goal_id uuid NOT NULL REFERENCES app.career_goals(id) ON DELETE CASCADE, goal_version integer NOT NULL,
 seq integer NOT NULL, request_id text NOT NULL, request_hash text NOT NULL, source text NOT NULL,
 source_sha256 text NOT NULL, input_payload jsonb NOT NULL,
 status text NOT NULL DEFAULT 'PENDING' CHECK(status IN ('PENDING','RUNNING','SUCCEEDED','FAILED','CANCELLED')),
 attempt integer NOT NULL DEFAULT 0 CHECK(attempt BETWEEN 0 AND 3), fencing_token bigint NOT NULL DEFAULT 0,
 execution_started boolean NOT NULL DEFAULT false, lease_until timestamptz,
 dispatched_at timestamptz, dispatch_count integer NOT NULL DEFAULT 0,
 candidate jsonb, result jsonb, error_code text, created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 UNIQUE(session_id,request_id), UNIQUE(session_id,seq), CHECK((status='RUNNING')=(lease_until IS NOT NULL))
);
CREATE INDEX career_written_dispatch_idx ON app.career_written_submissions(status,lease_until,dispatched_at);
CREATE FUNCTION app.career_written_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF (NEW.goal_id,NEW.goal_version,NEW.request_id,NEW.request_hash,NEW.input_payload) IS DISTINCT FROM
    (OLD.goal_id,OLD.goal_version,OLD.request_id,OLD.request_hash,OLD.input_payload) THEN
  RAISE EXCEPTION 'written_input_immutable' USING ERRCODE='23514';
 END IF;
 IF TG_TABLE_NAME='career_written_submissions' THEN
  IF (NEW.session_id,NEW.seq,NEW.job_id,NEW.source,NEW.source_sha256) IS DISTINCT FROM
     (OLD.session_id,OLD.seq,OLD.job_id,OLD.source,OLD.source_sha256) THEN
   RAISE EXCEPTION 'written_submission_immutable' USING ERRCODE='23514';
  END IF;
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER written_session_input_immutable BEFORE UPDATE ON app.career_written_sessions FOR EACH ROW EXECUTE FUNCTION app.career_written_immutable();
CREATE TRIGGER written_submission_input_immutable BEFORE UPDATE ON app.career_written_submissions FOR EACH ROW EXECUTE FUNCTION app.career_written_immutable();
GRANT SELECT,INSERT,UPDATE,DELETE ON app.career_written_sessions,app.career_written_submissions TO py_interview_app;
