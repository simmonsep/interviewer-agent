CREATE TABLE app.career_goals (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), user_id uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
 request_id text NOT NULL, request_hash text NOT NULL, version integer NOT NULL DEFAULT 1,
 revision bigint NOT NULL DEFAULT 1, status text NOT NULL DEFAULT 'PENDING'
 CHECK(status IN ('PENDING','RUNNING','WAITING_FOR_USER','WAITING_FOR_TOOL','FAILED','CANCELLED','COMPLETED','WAITING_CREDENTIALS')),
 conditions jsonb NOT NULL, jd_text text NOT NULL DEFAULT '', profile jsonb, action jsonb,
 model_requests integer NOT NULL DEFAULT 0 CHECK(model_requests BETWEEN 0 AND 12),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(), updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 UNIQUE(user_id,request_id), CHECK(version>=1 AND revision>=1)
);
CREATE TABLE app.career_steps (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), job_id uuid NOT NULL UNIQUE DEFAULT gen_random_uuid(),
 goal_id uuid NOT NULL REFERENCES app.career_goals(id) ON DELETE CASCADE, goal_version integer NOT NULL,
 request_id text NOT NULL, request_hash text NOT NULL, input_payload jsonb NOT NULL,
 message text NOT NULL CHECK(length(message) BETWEEN 1 AND 2000),
 status text NOT NULL DEFAULT 'PENDING' CHECK(status IN ('PENDING','RUNNING','SUCCEEDED','FAILED','CANCELLED','WAITING_CREDENTIALS')),
 attempt integer NOT NULL DEFAULT 0 CHECK(attempt BETWEEN 0 AND 3), fencing_token bigint NOT NULL DEFAULT 0,
 lease_until timestamptz, dispatched_at timestamptz, dispatch_count integer NOT NULL DEFAULT 0,
 model_requests integer NOT NULL DEFAULT 0 CHECK(model_requests BETWEEN 0 AND 3),
 candidate jsonb, tool_log jsonb, error_code text, origin text CHECK(origin IN ('scripted_protocol','real_model')),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 UNIQUE(goal_id,request_id), UNIQUE(goal_id,goal_version),
 CHECK((status='RUNNING')=(lease_until IS NOT NULL))
);
CREATE INDEX career_goals_owner_idx ON app.career_goals(user_id,created_at DESC);
CREATE INDEX career_steps_dispatch_idx ON app.career_steps(status,dispatched_at,lease_until);
CREATE FUNCTION app.career_input_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF (NEW.goal_id,NEW.goal_version,NEW.request_id,NEW.request_hash,NEW.message,NEW.input_payload)
  IS DISTINCT FROM (OLD.goal_id,OLD.goal_version,OLD.request_id,OLD.request_hash,OLD.message,OLD.input_payload) THEN
  RAISE EXCEPTION 'career_input_immutable' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER career_input_immutable BEFORE UPDATE ON app.career_steps
 FOR EACH ROW EXECUTE FUNCTION app.career_input_immutable();
ALTER TABLE app.model_http_calls ADD COLUMN purpose text NOT NULL DEFAULT 'assessment'
 CHECK(purpose IN ('assessment','career_coordination'));
GRANT SELECT,INSERT,UPDATE,DELETE ON app.career_goals,app.career_steps TO py_interview_app;
