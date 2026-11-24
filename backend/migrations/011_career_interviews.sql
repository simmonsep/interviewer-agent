ALTER TABLE app.career_goals DROP CONSTRAINT career_goals_model_requests_check;
ALTER TABLE app.career_goals ADD CHECK(model_requests BETWEEN 0 AND 72);
ALTER TABLE app.career_goals ADD COLUMN coordination_research_requests integer NOT NULL DEFAULT 0 CHECK(coordination_research_requests BETWEEN 0 AND 12);
UPDATE app.career_goals SET coordination_research_requests=model_requests-material_model_requests;
ALTER TABLE app.career_goals ADD COLUMN training_model_requests integer NOT NULL DEFAULT 0 CHECK(training_model_requests BETWEEN 0 AND 48);
ALTER TABLE app.model_http_calls DROP CONSTRAINT model_http_calls_purpose_check;
ALTER TABLE app.model_http_calls ADD CHECK(purpose IN ('assessment','career_coordination','career_research','career_materials','career_interview'));
CREATE TABLE app.career_interview_memory_state (
 goal_id uuid PRIMARY KEY REFERENCES app.career_goals(id) ON DELETE CASCADE, revision integer NOT NULL DEFAULT 0
);
CREATE TABLE app.career_interviews (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), goal_id uuid NOT NULL REFERENCES app.career_goals(id) ON DELETE CASCADE,
 goal_version integer NOT NULL, material_revision integer NOT NULL, memory_revision integer NOT NULL,
 request_id text NOT NULL, request_hash text NOT NULL, input_payload jsonb NOT NULL,
 version integer NOT NULL DEFAULT 1, revision bigint NOT NULL DEFAULT 1,
 status text NOT NULL DEFAULT 'PENDING' CHECK(status IN ('PENDING','RUNNING','WAITING_FOR_USER','COMPLETED','CANCELLED','FAILED','WAITING_CREDENTIALS','STALE')),
 question_count integer NOT NULL DEFAULT 0, model_requests integer NOT NULL DEFAULT 0 CHECK(model_requests BETWEEN 0 AND 32),
 deadline_at timestamptz NOT NULL, stop_reason text, model_summary text,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(), UNIQUE(goal_id,request_id)
);
CREATE TABLE app.career_interview_runs (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), job_id uuid NOT NULL UNIQUE DEFAULT gen_random_uuid(),
 goal_id uuid NOT NULL REFERENCES app.career_goals(id) ON DELETE CASCADE, goal_version integer NOT NULL,
 session_id uuid NOT NULL REFERENCES app.career_interviews(id) ON DELETE CASCADE, session_version integer NOT NULL,
 input_payload jsonb NOT NULL,
 status text NOT NULL DEFAULT 'PENDING' CHECK(status IN ('PENDING','RUNNING','SUCCEEDED','FAILED','CANCELLED','WAITING_CREDENTIALS')),
 revision bigint NOT NULL DEFAULT 1, attempt integer NOT NULL DEFAULT 0 CHECK(attempt BETWEEN 0 AND 3), fencing_token bigint NOT NULL DEFAULT 0,
 lease_until timestamptz, dispatched_at timestamptz, dispatch_count integer NOT NULL DEFAULT 0,
 model_requests integer NOT NULL DEFAULT 0 CHECK(model_requests BETWEEN 0 AND 4), candidate jsonb, result jsonb, error_code text,
 origin text CHECK(origin IN ('scripted_protocol','real_model')),created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 UNIQUE(session_id,session_version), CHECK((status='RUNNING')=(lease_until IS NOT NULL))
);
CREATE INDEX career_interview_dispatch_idx ON app.career_interview_runs(status,dispatched_at,lease_until);
CREATE TABLE app.career_interview_events (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), session_id uuid NOT NULL REFERENCES app.career_interviews(id) ON DELETE CASCADE,
 seq integer NOT NULL, kind text NOT NULL CHECK(kind IN ('question','answer','explanation')),
 request_id text, request_hash text, payload jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 UNIQUE(session_id,seq), UNIQUE(session_id,request_id)
);
CREATE TABLE app.career_interview_observations (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), goal_id uuid NOT NULL REFERENCES app.career_goals(id) ON DELETE CASCADE,
 session_id uuid NOT NULL REFERENCES app.career_interviews(id) ON DELETE CASCADE,
 answer_id uuid NOT NULL UNIQUE REFERENCES app.career_interview_events(id) ON DELETE CASCADE,
 payload jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE app.career_interview_corrections (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), goal_id uuid NOT NULL REFERENCES app.career_goals(id) ON DELETE CASCADE,
 observation_id uuid NOT NULL UNIQUE REFERENCES app.career_interview_observations(id) ON DELETE CASCADE,
 request_id text NOT NULL, request_hash text NOT NULL, reason text NOT NULL, created_at timestamptz NOT NULL DEFAULT clock_timestamp(), UNIQUE(goal_id,request_id)
);
CREATE FUNCTION app.career_interview_input_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF (NEW.goal_id,NEW.goal_version,NEW.input_payload) IS DISTINCT FROM (OLD.goal_id,OLD.goal_version,OLD.input_payload) THEN
  RAISE EXCEPTION 'career_interview_input_immutable' USING ERRCODE='23514';
 END IF;
 IF TG_TABLE_NAME='career_interview_runs' THEN
  IF (NEW.session_id,NEW.session_version,NEW.job_id) IS DISTINCT FROM (OLD.session_id,OLD.session_version,OLD.job_id) THEN
   RAISE EXCEPTION 'career_interview_run_binding_immutable' USING ERRCODE='23514';
  END IF;
 ELSE
  IF (NEW.material_revision,NEW.memory_revision,NEW.request_id,NEW.request_hash) IS DISTINCT FROM (OLD.material_revision,OLD.memory_revision,OLD.request_id,OLD.request_hash) THEN
   RAISE EXCEPTION 'career_interview_binding_immutable' USING ERRCODE='23514';
  END IF;
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER career_interview_input_immutable BEFORE UPDATE ON app.career_interviews FOR EACH ROW EXECUTE FUNCTION app.career_interview_input_immutable();
CREATE TRIGGER career_interview_run_input_immutable BEFORE UPDATE ON app.career_interview_runs FOR EACH ROW EXECUTE FUNCTION app.career_interview_input_immutable();
CREATE TRIGGER career_interview_event_immutable BEFORE UPDATE ON app.career_interview_events FOR EACH ROW EXECUTE FUNCTION app.career_snapshot_immutable();
CREATE TRIGGER career_interview_observation_immutable BEFORE UPDATE ON app.career_interview_observations FOR EACH ROW EXECUTE FUNCTION app.career_snapshot_immutable();
CREATE TRIGGER career_interview_correction_immutable BEFORE UPDATE ON app.career_interview_corrections FOR EACH ROW EXECUTE FUNCTION app.career_snapshot_immutable();
GRANT SELECT,INSERT,UPDATE,DELETE ON app.career_interviews,app.career_interview_runs,app.career_interview_memory_state TO py_interview_app;
GRANT SELECT,INSERT,DELETE ON app.career_interview_events,app.career_interview_observations,app.career_interview_corrections TO py_interview_app;
