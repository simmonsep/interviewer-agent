CREATE TABLE app.career_research_runs (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), job_id uuid NOT NULL UNIQUE DEFAULT gen_random_uuid(),
 goal_id uuid NOT NULL REFERENCES app.career_goals(id) ON DELETE CASCADE, goal_version integer NOT NULL,
 request_id text NOT NULL, request_hash text NOT NULL, input_payload jsonb NOT NULL,
 status text NOT NULL DEFAULT 'PENDING' CHECK(status IN ('PENDING','RUNNING','SUCCEEDED','PARTIAL','FAILED','CANCELLED')),
 revision bigint NOT NULL DEFAULT 1, attempt integer NOT NULL DEFAULT 0 CHECK(attempt BETWEEN 0 AND 3),
 fencing_token bigint NOT NULL DEFAULT 0, lease_until timestamptz,
 dispatched_at timestamptz, dispatch_count integer NOT NULL DEFAULT 0,
 candidate jsonb, result jsonb, error_code text,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 UNIQUE(goal_id,request_id), CHECK(goal_version>=1 AND revision>=1),
 CHECK((status='RUNNING')=(lease_until IS NOT NULL))
);
CREATE INDEX career_research_dispatch_idx ON app.career_research_runs(status,dispatched_at,lease_until);
CREATE TABLE app.career_job_snapshots (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 goal_id uuid NOT NULL REFERENCES app.career_goals(id) ON DELETE CASCADE,
 source_id text NOT NULL CHECK(source_id IN ('tme-campus','xiaomi-intern','user-jd')),
 source_job_id text NOT NULL, content_sha256 text NOT NULL CHECK(length(content_sha256)=64),
 source_url text, retrieval_url text, payload jsonb NOT NULL,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 UNIQUE(goal_id,source_id,source_job_id,content_sha256)
);
CREATE TABLE app.career_job_observations (
 run_id uuid NOT NULL REFERENCES app.career_research_runs(id) ON DELETE CASCADE,
 snapshot_id uuid NOT NULL REFERENCES app.career_job_snapshots(id) ON DELETE CASCADE,
 observed_at timestamptz NOT NULL, response_sha256 text,
 city_check text NOT NULL CHECK(city_check IN ('match','conflict','unknown','not_requested')),
 included boolean NOT NULL, PRIMARY KEY(run_id,snapshot_id)
);
CREATE FUNCTION app.career_research_input_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF (NEW.goal_id,NEW.goal_version,NEW.request_id,NEW.request_hash,NEW.input_payload)
 IS DISTINCT FROM (OLD.goal_id,OLD.goal_version,OLD.request_id,OLD.request_hash,OLD.input_payload) THEN
  RAISE EXCEPTION 'career_research_input_immutable' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER career_research_input_immutable BEFORE UPDATE ON app.career_research_runs
 FOR EACH ROW EXECUTE FUNCTION app.career_research_input_immutable();
CREATE FUNCTION app.career_snapshot_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 RAISE EXCEPTION 'career_snapshot_immutable' USING ERRCODE='23514';
END $$;
CREATE TRIGGER career_snapshot_immutable BEFORE UPDATE ON app.career_job_snapshots
 FOR EACH ROW EXECUTE FUNCTION app.career_snapshot_immutable();
GRANT SELECT,INSERT,UPDATE,DELETE ON app.career_research_runs TO py_interview_app;
GRANT SELECT,INSERT,DELETE ON app.career_job_snapshots,app.career_job_observations TO py_interview_app;
